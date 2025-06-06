/*************************************************************************
 * Copyright (c) 2022-2025, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 *
 * See LICENSE for license information.
 ************************************************************************/

#include <cuda.h>
#include <cudaTypedefs.h>
#include <cuda_runtime.h>
#include <transformer_engine/cast.h>
#include <transformer_engine/multi_stream.h>

#include <cfloat>
#include <limits>
#include <mutex>
#include <string>

#include "../common.h"
#include "../transpose/cast_transpose.h"
#include "../util/multi_stream.h"
#include "../util/vectorized_pointwise.h"
#include "../utils.cuh"
#include "cast_kernels.cuh"
#include "dequantize_kernels.cuh"
#include "math.h"
#include "ptx.cuh"
#include "transformer_engine/activation.h"
#include "transformer_engine/transpose.h"

void nvte_quantize(const NVTETensor input, NVTETensor output, cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = false;
  constexpr bool IS_DACT = false;
  constexpr bool IS_ACT = false;
  constexpr NVTETensor dbias = nullptr;
  constexpr NVTETensor workspace = nullptr;
  constexpr const NVTETensor grad = nullptr;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, nullptr>(input, grad, output, dbias,
                                                                     workspace, nullptr, stream);
}

void nvte_quantize_noop(const NVTETensor input, NVTETensor output, NVTETensor noop,
                        cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_noop);
  using namespace transformer_engine;

  // Create config with noop tensor
  QuantizationConfig quant_config;
  quant_config.noop_tensor = noop;

  nvte_quantize_v2(input, output, reinterpret_cast<NVTEQuantizationConfig>(&quant_config), stream);
}

void nvte_quantize_v2(const NVTETensor input, NVTETensor output,
                      const NVTEQuantizationConfig quant_config, cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_v2);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = false;
  constexpr bool IS_DACT = false;
  constexpr bool IS_ACT = false;
  constexpr NVTETensor dbias = nullptr;
  constexpr NVTETensor workspace = nullptr;
  constexpr const NVTETensor grad = nullptr;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, nullptr>(
      input, grad, output, dbias, workspace, quant_config, stream);
}

void nvte_quantize_dbias(const NVTETensor input, NVTETensor output, NVTETensor dbias,
                         NVTETensor workspace, cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_dbias);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = true;
  constexpr bool IS_DACT = false;
  constexpr bool IS_ACT = false;
  constexpr const NVTETensor activation_input = nullptr;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, nullptr>(
      activation_input, input, output, dbias, workspace, nullptr, stream);
}

void nvte_quantize_dbias_dgelu(const NVTETensor input, const NVTETensor activation_input,
                               NVTETensor output, NVTETensor dbias, NVTETensor workspace,
                               cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_dbias_dgelu);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = true;
  constexpr bool IS_DACT = true;
  constexpr bool IS_ACT = false;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, dgelu<fp32, fp32>>(
      activation_input, input, output, dbias, workspace, nullptr, stream);
}

void nvte_quantize_dbias_dsilu(const NVTETensor input, const NVTETensor activation_input,
                               NVTETensor output, NVTETensor dbias, NVTETensor workspace,
                               cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_dbias_dsilu);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = true;
  constexpr bool IS_DACT = true;
  constexpr bool IS_ACT = false;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, dsilu<fp32, fp32>>(
      activation_input, input, output, dbias, workspace, nullptr, stream);
}

void nvte_quantize_dbias_drelu(const NVTETensor input, const NVTETensor activation_input,
                               NVTETensor output, NVTETensor dbias, NVTETensor workspace,
                               cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_dbias_drelu);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = true;
  constexpr bool IS_DACT = true;
  constexpr bool IS_ACT = false;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, drelu<fp32, fp32>>(
      activation_input, input, output, dbias, workspace, nullptr, stream);
}

void nvte_quantize_dbias_dqgelu(const NVTETensor input, const NVTETensor activation_input,
                                NVTETensor output, NVTETensor dbias, NVTETensor workspace,
                                cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_dbias_dqgelu);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = true;
  constexpr bool IS_DACT = true;
  constexpr bool IS_ACT = false;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, dqgelu<fp32, fp32>>(
      activation_input, input, output, dbias, workspace, nullptr, stream);
}

void nvte_quantize_dbias_dsrelu(const NVTETensor input, const NVTETensor activation_input,
                                NVTETensor output, NVTETensor dbias, NVTETensor workspace,
                                cudaStream_t stream) {
  NVTE_API_CALL(nvte_quantize_dbias_dsrelu);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = true;
  constexpr bool IS_DACT = true;
  constexpr bool IS_ACT = false;

  detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, dsrelu<fp32, fp32>>(
      activation_input, input, output, dbias, workspace, nullptr, stream);
}

void nvte_dequantize(const NVTETensor input, NVTETensor output, cudaStream_t stream) {
  NVTE_API_CALL(nvte_dequantize);
  using namespace transformer_engine;
  detail::dequantize_helper(*convertNVTETensorCheck(input), convertNVTETensorCheck(output), stream);
}

void nvte_multi_tensor_quantize(const NVTETensor *inputs, NVTETensor *outputs,
                                const NVTEQuantizationConfig quant_configs,
                                const size_t num_tensors, cudaStream_t stream) {
  NVTE_API_CALL(nvte_multi_tensor_quantize);
  using namespace transformer_engine;

  constexpr bool IS_DBIAS = false;
  constexpr bool IS_DACT = false;
  constexpr bool IS_ACT = false;
  constexpr NVTETensor dbias = nullptr;
  constexpr NVTETensor workspace = nullptr;
  constexpr const NVTETensor grad = nullptr;

  const size_t num_streams = nvte_get_num_compute_streams();

  int num_stream_used = std::min(num_streams, num_tensors);
  // wait for current stream to finish
  NVTE_CHECK_CUDA(cudaEventRecord(detail::get_compute_stream_event(0), stream));
  for (int s = 0; s < num_stream_used; s++) {
    NVTE_CHECK_CUDA(
        cudaStreamWaitEvent(detail::get_compute_stream(s), detail::get_compute_stream_event(0)));
  }

  for (int i = 0; i < num_tensors; i++) {
    detail::quantize_helper<IS_DBIAS, IS_DACT, IS_ACT, Empty, nullptr>(
        inputs[i], grad, outputs[i], dbias, workspace, nullptr,
        detail::get_compute_stream(i % num_streams));
  }

  // record events on compute streams
  for (int s = 0; s < num_stream_used; s++) {
    NVTE_CHECK_CUDA(
        cudaEventRecord(detail::get_compute_stream_event(s), detail::get_compute_stream(s)));
  }
  // wait for all compute streams to finish
  for (int s = 0; s < num_stream_used; s++) {
    NVTE_CHECK_CUDA(cudaStreamWaitEvent(stream, detail::get_compute_stream_event(s)));
  }
}
