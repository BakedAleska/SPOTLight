source /root/env.sh
ROCM="$(rocm-sdk path --root)"
echo "ROCM=$ROCM hipconfig -R=$(hipconfig -R)"
cd /root/llama.cpp && git log --oneline -1 && rm -rf build
HIPCXX="$ROCM/lib/llvm/bin/clang" HIP_PATH="$ROCM" ROCM_PATH="$ROCM" \
  cmake -S . -B build -G Ninja -DGGML_HIP=ON -DGPU_TARGETS=gfx1100 -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$ROCM" && cmake --build build -j 16
echo "BUILD_EXIT=$?"
ls -l build/bin/llama-server
