# The only image in the hive that carries torch and CUDA. Everything the
# control plane does — planning, launching, recording, deploying — happens
# in stdlib Python elsewhere; this image exists so none of that has to
# install a multi-gigabyte wheel it uses once.
#
# Built from PyTorch's CUDA image rather than assembled from pip, because
# bitsandbytes and the torch build have to agree on a CUDA version and
# getting that wrong surfaces as a kernel error twenty minutes into a run.
FROM pytorch/pytorch:2.9.0-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface

# git and build tooling are for llama.cpp's converter, used by merge mode.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git build-essential cmake curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# Converter and quantizer for merge mode. Only the conversion tooling is
# built — inference lives in Ollama, not here.
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp && \
    pip install --no-cache-dir -r /opt/llama.cpp/requirements/requirements-convert_hf_to_gguf.txt && \
    cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -DLLAMA_CURL=OFF -DGGML_NATIVE=ON && \
    cmake --build /opt/llama.cpp/build --target llama-quantize -j "$(nproc)" && \
    cp /opt/llama.cpp/build/bin/llama-quantize /opt/llama.cpp/llama-quantize && \
    rm -rf /opt/llama.cpp/build

COPY requirements.txt /opt/trainer/requirements.txt
RUN pip install --no-cache-dir -r /opt/trainer/requirements.txt

COPY train.py /opt/trainer/train.py

WORKDIR /workspace
ENTRYPOINT ["python", "/opt/trainer/train.py"]
