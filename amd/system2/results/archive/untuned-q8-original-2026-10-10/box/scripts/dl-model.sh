source /root/env.sh
pip install -q -U "huggingface_hub[cli]"
HF_HUB_DISABLE_XET=1 HF_HUB_ENABLE_HF_TRANSFER=0 HF_ENDPOINT=https://hf-mirror.com hf download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-Q8_0.gguf mmproj-F16.gguf \
  --revision 4ca720788d1e01f1bff70c033e0d0028fd02e502 --local-dir /root/models/q38-27b
echo "DL_EXIT=$?"
cd /root/models/q38-27b && sha256sum -c - <<"EOS"
a680f44a06920e5d689774823782006aa3acc8db95750323373b24139b67e348  Qwen3.8-27B-Q8_0.gguf
cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e  mmproj-F16.gguf
EOS
echo "SHA_EXIT=$?"
