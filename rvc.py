import os
import sys
import argparse

def list_models(dir):
    f = False
    i = 0
    for filename in os.listdir(dir):
        if filename.endswith('.pth'):
            print(f"{i+1}. {filename}")
            f = True
            i+=1
    if not f:
        print("No Models installed")
    sys.exit(0)

if __name__ == '__main__':
  class ArgvProxy:
    def __init__(self, original_argv):
      self._org_argv = original_argv
      parser = argparse.ArgumentParser()
      parser.add_argument("-i", "--input", help="Input audio file name", type=str, default='')
      parser.add_argument("-o", "--output", help="Output audio file name", type=str, default='')
      parser.add_argument("-m", "--model", help="Model file name", type=str)
      parser.add_argument("-s", "--speaker-id", help="Speaker ID", type=int, default=0)
      parser.add_argument("-e", "--embedder", help="[Embedder Model];[Embedder Output Layer]", type=str, default="auto;auto")
      parser.add_argument("-t", "--transpose", help="F0 up key", type=int, default=0)
      parser.add_argument('-F', '--f0-file', type=str, default='')
      parser.add_argument('-f', '--f0-method', type=str, default='crepe')
      parser.add_argument('-I', '--auto-index-load', type=bool, default=False)
      parser.add_argument('--faiss-index-file', type=str, default='')
      parser.add_argument('--retrieval-feature-ratio', type=float, default=1.0)
      parser.add_argument('--list-models', help='Show installed model name', action='store_true')
      parser.add_argument('--use-cpu', action='store_true')
      if len(self._org_argv) <= 1:
        self._org_argv.append('--help')
      opts, unknown = parser.parse_known_args()
      self._opts = vars(opts)
      self._argv = [self._org_argv[0]] + unknown

      self.default_set()
    def __getitem__(self, index):
      return self._argv[index]

    def __len__(self):
      return len(self._argv)

    def __iter__(self):
      return iter(self._argv)

    def __repr__(self):
      return repr(self._argv)
    
    def get(self, key):
      return self._opts[key]
    def default_set(self):
        if self._opts['output'] == '' and self._opts['input'] != '':
          base = os.path.basename(self._opts['input'])
          basename, _ = os.path.splitext(base)
          self._opts['output'] = basename + '_by_' + self._opts['model'] + '.wav'
    

  # sys.argv を差し替え
  sys.argv = ArgvProxy(sys.argv)


MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
import torch
from modules.shared import device, is_half

if sys.argv.get('use_cpu'):
    device = torch.device('cpu')

import requests
from requests.exceptions import ConnectionError
from modules.core import preload
try:
    preload()
except ConnectionError as e:
    print("Network is not available. Skip preload()")


import re
from typing import *

from fairseq import checkpoint_utils
from fairseq.models.hubert.hubert import HubertModel
from pydub import AudioSegment

from lib.rvc.models import (SynthesizerTrnMs256NSFSid,
                            SynthesizerTrnMs256NSFSidNono)
from lib.rvc.pipeline import VocalConvertPipeline

from modules.cmd_opts import opts
from modules.utils import load_audio



EMBEDDINGS_LIST = {
    "hubert-base-japanese": (
        "rinna_hubert_base_jp.pt",
        "hubert-base-japanese",
        "local",
    ),
    "contentvec": ("checkpoint_best_legacy_500.pt", "contentvec", "local"),
}


def update_state_dict(state_dict):
    if "params" in state_dict and state_dict["params"] is not None:
        return
    keys = [
        "spec_channels",
        "segment_size",
        "inter_channels",
        "hidden_channels",
        "filter_channels",
        "n_heads",
        "n_layers",
        "kernel_size",
        "p_dropout",
        "resblock",
        "resblock_kernel_sizes",
        "resblock_dilation_sizes",
        "upsample_rates",
        "upsample_initial_channel",
        "upsample_kernel_sizes",
        "spk_embed_dim",
        "gin_channels",
        "emb_channels",
        "sr",
    ]
    state_dict["params"] = {}
    n = 0
    for i, key in enumerate(keys):
        i = i - n
        if len(state_dict["config"]) != 19 and key == "emb_channels":
            # backward compat.
            n += 1
            continue
        state_dict["params"][key] = state_dict["config"][i]

    if not "emb_channels" in state_dict["params"]:
        if state_dict.get("version", "v1") == "v1":
            state_dict["params"]["emb_channels"] = 256  # for backward compat.
            state_dict["embedder_output_layer"] = 9
        else:
            state_dict["params"]["emb_channels"] = 768  # for backward compat.
            state_dict["embedder_output_layer"] = 12


class VoiceConvertModel:
    def __init__(self, model_name: str, state_dict: Dict[str, Any]) -> None:
        update_state_dict(state_dict)
        self.model_name = model_name
        self.state_dict = state_dict
        self.tgt_sr = state_dict["params"]["sr"]
        f0 = state_dict.get("f0", 1)
        state_dict["params"]["spk_embed_dim"] = state_dict["weight"][
            "emb_g.weight"
        ].shape[0]
        if not "emb_channels" in state_dict["params"]:
            state_dict["params"]["emb_channels"] = 256  # for backward compat.

        if f0 == 1:
            self.net_g = SynthesizerTrnMs256NSFSid(
                **state_dict["params"], is_half=is_half
            )
        else:
            self.net_g = SynthesizerTrnMs256NSFSidNono(**state_dict["params"])

        del self.net_g.enc_q

        self.net_g.load_state_dict(state_dict["weight"], strict=False)
        self.net_g.eval().to(device)

        if is_half:
            self.net_g = self.net_g.half()
        else:
            self.net_g = self.net_g.float()

        self.vc = VocalConvertPipeline(self.tgt_sr, device, is_half)
        self.n_spk = state_dict["params"]["spk_embed_dim"]

    def single(
        self,
        sid: int,
        input_audio: str,
        embedder_model_name: str,
        embedding_output_layer: str,
        f0_up_key: int,
        f0_file: str,
        f0_method: str,
        auto_load_index: bool,
        faiss_index_file: str,
        index_rate: float,
        output_file: str,
    ):
        if not input_audio:
            raise Exception("You need to set Source Audio")
        f0_up_key = int(f0_up_key)
        audio = load_audio(input_audio, 16000)

        if embedder_model_name == "auto":
            embedder_model_name = (
                self.state_dict["embedder_name"]
                if "embedder_name" in self.state_dict
                else "hubert_base"
            )
            if embedder_model_name.endswith("768"):
                embedder_model_name = embedder_model_name[:-3]

        if embedder_model_name == "hubert_base":
            embedder_model_name = "contentvec"

        if not embedder_model_name in EMBEDDINGS_LIST.keys():
            raise Exception(f"Not supported embedder: {embedder_model_name}")

        if (
            embedder_model == None
            or loaded_embedder_model != EMBEDDINGS_LIST[embedder_model_name][1]
        ):
            print(f"load {embedder_model_name} embedder")
            embedder_filename, embedder_name, load_from = get_embedder(
                embedder_model_name
            )
            load_embedder(embedder_filename, embedder_name)

        if embedding_output_layer == "auto":
            embedding_output_layer = (
                self.state_dict["embedding_output_layer"]
                if "embedding_output_layer" in self.state_dict
                else 12
            )
        else:
            embedding_output_layer = int(embedding_output_layer)

        f0 = self.state_dict.get("f0", 1)

        if not faiss_index_file and auto_load_index:
            faiss_index_file = self.get_index_path(sid)

        audio_opt = self.vc(
            embedder_model,
            embedding_output_layer,
            self.net_g,
            sid,
            audio,
            f0_up_key,
            f0_method,
            faiss_index_file,
            index_rate,
            f0,
            f0_file=f0_file,
        )

        audio = AudioSegment(
            audio_opt,
            frame_rate=self.tgt_sr,
            sample_width=2,
            channels=1,
        )
        input_audio_splitext = os.path.splitext(os.path.basename(input_audio))[0]
        model_splitext = os.path.splitext(self.model_name)[0]
        index = 0
        audio.export(
            output_file,
            format="wav",
        )
        return audio_opt

    def get_index_path(self, speaker_id: int):
        basename = os.path.splitext(self.model_name)[0]
        speaker_index_path = os.path.join(
            MODELS_DIR,
            "checkpoints",
            f"{basename}_index",
            f"{basename}.{speaker_id}.index",
        )
        if os.path.exists(speaker_index_path):
            return speaker_index_path
        return os.path.join(MODELS_DIR, "checkpoints", f"{basename}.index")



vc_model: Optional[VoiceConvertModel] = None
embedder_model: Optional[HubertModel] = None
loaded_embedder_model = ""


def get_models():
    dir = os.path.join(ROOT_DIR, "models", "checkpoints")
    os.makedirs(dir, exist_ok=True)
    return [
        file
        for file in os.listdir(dir)
        if any([x for x in [".ckpt", ".pth"] if file.endswith(x)])
    ]


def get_embedder(embedder_name):
    if embedder_name in EMBEDDINGS_LIST:
        return EMBEDDINGS_LIST[embedder_name]
    return None


def load_embedder(emb_file: str, emb_name: str):
    global embedder_model, loaded_embedder_model
    emb_file = os.path.join(MODELS_DIR, "embeddings", emb_file)
    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        [emb_file],
        suffix="",
    )
    embedder_model = models[0]
    embedder_model = embedder_model.to(device)

    if is_half:
        embedder_model = embedder_model.half()
    else:
        embedder_model = embedder_model.float()
    embedder_model.eval()

    loaded_embedder_model = emb_name


def get_vc_model(model_name: str):
    model_path = os.path.join(MODELS_DIR, "checkpoints", model_name)
    import fairseq.data.dictionary
    torch.serialization.add_safe_globals([fairseq.data.dictionary.Dictionary])
    weight = torch.load(model_path,  map_location="cpu", weights_only=False)
    return VoiceConvertModel(model_name, weight)




if __name__ == '__main__':
  model = get_vc_model(sys.argv.get('model')+'.pth')
  embedder_name, embedder_output = sys.argv.get('embedder').split(';')
  model.single(
    sys.argv.get('speaker_id'),
    sys.argv.get('input'),
    embedder_name,
    embedder_output,
    sys.argv.get('transpose'),
    sys.argv.get('f0_file'),
    sys.argv.get('f0_method'),
    sys.argv.get('auto_index_load'),
    sys.argv.get('faiss_index_file'),
    sys.argv.get('retrieval_feature_ratio'),
    sys.argv.get('output')
     )
