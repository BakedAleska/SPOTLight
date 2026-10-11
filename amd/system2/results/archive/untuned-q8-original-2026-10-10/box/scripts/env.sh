export HTTPS_PROXY=http://notebook-egress-proxy.amd-oneclick-lablab.svc.cluster.local:3128
export HTTP_PROXY=$HTTPS_PROXY https_proxy=$HTTPS_PROXY http_proxy=$HTTPS_PROXY
export HF_HOME=/root/.cache/huggingface TMPDIR=/root/tmp
mkdir -p $TMPDIR /root/models
source /opt/venv/bin/activate
export no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost
export LD_LIBRARY_PATH=/opt/venv/lib/python3.14/site-packages/_rocm_sdk_devel/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
