"""Ship a fine-tuned Laya checkpoint as a small patch against base Laya (uploads to the AMD box run ~16 KB/s per
connection, so the 816 MB merged model.safetensors is impractical; the patch is ~81 MB). Used by laptop/push_laya.sh.

  make : laya_patch.py make  <ft model.safetensors> <base model.safetensors> <adapter dir> <out patch.safetensors>
  apply: laya_patch.py apply <base model.safetensors> <patch.safetensors> <out model.safetensors>

LoRA-target matrices are rebuilt as fp16(base_fp32 + (B @ A) * alpha/r) in fp32, PEFT's merge; every other changed
tensor (trained norms, head, scorer) is stored as-is. The trainer merged on the GPU, so rebuilt matrices can differ by
one fp16 ulp; on run4_B_lora_lr4 that moved no M006 answer and no probability by more than 2e-6. `make` refuses a patch
whose rebuild differs by more than MAX_ABS_DIFF anywhere.
"""
import json, os, re, sys
import torch
from safetensors.torch import load_file, save_file

MAX_ABS_DIFF = 1e-3   # one fp16 ulp is <= 4.9e-4 for these weight magnitudes
TARGET = re.compile(r"^encoder\.layers\.(\d+)\.(attn\.Wqkv|attn\.Wo|mlp\.Wi|mlp\.Wo)\.weight$")

def rebuild(base, patch):
    scale = float(patch["meta::scale"].item())
    out = {}
    for k, v in base.items():
        if f"full::{k}" in patch:
            out[k] = patch[f"full::{k}"]
        elif f"lora_A::{k}" in patch:
            a, b = patch[f"lora_A::{k}"], patch[f"lora_B::{k}"]
            out[k] = (v.float() + (b @ a) * scale).half()   # PEFT's merge: fp32, (B @ A) * scaling
        else:
            out[k] = v.half()
    return out

def make(ft_path, base_path, adapter_dir, out_path):
    ft, base = load_file(ft_path), load_file(base_path)
    cfg = json.load(open(os.path.join(adapter_dir, "adapter_config.json")))
    ad = load_file(os.path.join(adapter_dir, "adapter_model.safetensors"))
    patch = {"meta::scale": torch.tensor(cfg["lora_alpha"] / cfg["r"])}
    for k, v in ft.items():
        if torch.equal(base[k].half(), v):
            continue
        m = TARGET.match(k)
        if m:
            pre = f"base_model.model.layers.{m.group(1)}.{m.group(2)}"
            patch[f"lora_A::{k}"] = ad[f"{pre}.lora_A.weight"].contiguous()
            patch[f"lora_B::{k}"] = ad[f"{pre}.lora_B.weight"].contiguous()
        else:
            patch[f"full::{k}"] = v.contiguous()
    rb = rebuild(base, patch)
    worst, inexact = 0.0, 0
    for k, v in ft.items():
        if not torch.equal(rb[k], v):
            inexact += 1
            worst = max(worst, (rb[k].float() - v.float()).abs().max().item())
    print(f"tensors rebuilt inexactly: {inexact}, max abs diff {worst:.3g}")
    if worst > MAX_ABS_DIFF:
        sys.exit(f"rebuild differs by {worst:.3g} > {MAX_ABS_DIFF}: wrong base model or not a default-init LoRA run")
    save_file(patch, out_path)
    print(f"patch: {os.path.getsize(out_path)/1e6:.1f} MB, {len(patch)} entries")

def apply(base_path, patch_path, out_path):
    save_file({k: v.contiguous() for k, v in rebuild(load_file(base_path), load_file(patch_path)).items()}, out_path)
    print("wrote", out_path)

if __name__ == "__main__":
    {"make": make, "apply": apply}[sys.argv[1]](*sys.argv[2:])
