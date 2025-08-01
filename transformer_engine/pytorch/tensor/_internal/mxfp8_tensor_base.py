# Copyright (c) 2022-2025, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.

"""Mixin class holding data specific for MXFP8Tensor"""

from __future__ import annotations
from typing import Optional, Dict, Any, Tuple
from collections.abc import Iterable
import math
import torch

import transformer_engine_torch as tex
from transformer_engine_torch import DType as TE_DType

from ..quantized_tensor import QuantizedTensorBase

from ...constants import TE_DType as torch_to_transformer_engine_dtype

from ..quantized_tensor import Quantizer

from ...utils import _empty_tensor


class _FromMXFP8Func(torch.autograd.Function):
    """Cast from MXFP8 to other dtype"""

    @staticmethod
    def forward(
        _ctx: Optional[torch.autograd.function.FunctionCtx],  # unused
        tensor: MXFP8TensorBase,
        dtype: torch.dtype,
    ) -> torch.Tensor:
        # pylint: disable=missing-function-docstring
        dtype = torch_to_transformer_engine_dtype[dtype]

        # Make sure FP8 data is in expected format
        if tensor._rowwise_data is not None:
            return tex.dequantize(tensor, dtype)
        raise NotImplementedError("Casting back from the transpose not implemented yet!")

    @staticmethod
    def backward(
        _ctx: torch.autograd.function.FunctionCtx,  # unused
        grad: torch.Tensor,
    ) -> Tuple[Optional[torch.Tensor], ...]:
        # pylint: disable=missing-function-docstring
        # Assume that we want gradients in full precision
        return grad, None


class MXFP8TensorBase(QuantizedTensorBase):
    """Mixin class that holds data attributes of MXFP8Tensor.

    MXFP8Tensor inherits from the PyTorch tensor class and this mixin
    class. If this class is instantiated directly, it has the same
    data, lower CPU overhead, and less functionality. It should only
    be instantiated directly for performance-critical internal usage.

    """

    _rowwise_data: Optional[torch.Tensor]
    _columnwise_data: Optional[torch.Tensor]
    _quantizer: Optional[Quantizer]
    _fp8_dtype: TE_DType
    _rowwise_scale_inv: torch.Tensor
    _columnwise_scale_inv: torch.Tensor

    def __new__(
        cls,
        rowwise_data: Optional[torch.Tensor],
        rowwise_scale_inv: Optional[torch.Tensor],
        columnwise_data: Optional[torch.Tensor],
        columnwise_scale_inv: Optional[torch.Tensor],
        fp8_dtype: TE_DType,
        quantizer: Optional[Quantizer],
        *args,
        **kwargs,
    ):
        if cls is MXFP8TensorBase:
            instance = object.__new__(cls)
        else:
            instance = super().__new__(cls, *args, **kwargs)
        instance._rowwise_data = rowwise_data
        instance._columnwise_data = columnwise_data
        instance._quantizer = quantizer.copy() if quantizer is not None else None
        instance._fp8_dtype = fp8_dtype
        instance._rowwise_scale_inv = rowwise_scale_inv
        instance._columnwise_scale_inv = columnwise_scale_inv

        return instance

    def clear(self):
        """Deallocate this tensor's memory. Typically not needed and must be used carefully."""
        for t in (
            self._rowwise_data,
            self._columnwise_data,
            self._rowwise_scale_inv,
            self._columnwise_scale_inv,
        ):
            if t is not None:
                t.data = _empty_tensor()

    def get_metadata(self) -> Dict[str, Any]:
        """Get this tensor's metadata."""
        return {
            "rowwise_data": self._rowwise_data,
            "rowwise_scale_inv": self._rowwise_scale_inv,
            "columnwise_data": self._columnwise_data,
            "columnwise_scale_inv": self._columnwise_scale_inv,
            "fp8_dtype": self._fp8_dtype,
            "quantizer": self._quantizer,
        }

    def prepare_for_saving(self) -> Tuple[list[Optional[torch.Tensor]], MXFP8TensorBase]:
        """Prepare the tensor base for saving for backward"""
        tensors = [
            self._rowwise_data,
            self._columnwise_data,
            self._rowwise_scale_inv,
            self._columnwise_scale_inv,
        ]
        self._rowwise_data = None
        self._columnwise_data = None
        self._rowwise_scale_inv = None
        self._columnwise_scale_inv = None
        return tensors, self

    def restore_from_saved(
        self, tensors: list[Optional[torch.Tensor]]
    ) -> list[Optional[torch.Tensor]]:
        """Restore the tensor base data from the saved tensors list."""
        self._rowwise_data = tensors[0]
        self._columnwise_data = tensors[1]
        self._rowwise_scale_inv = tensors[2]
        self._columnwise_scale_inv = tensors[3]
        return tensors[4:]

    def get_data_tensors(self):
        """Get this Tensor's data."""
        return self._rowwise_data, self._columnwise_data

    def dequantize(self, *, dtype: torch.dtype = torch.float32) -> torch.Tensor:
        """Dequantize to a higher precision."""
        return _FromMXFP8Func.forward(None, self, dtype)

    def size(self, *args, **kwargs):
        # pylint: disable=missing-function-docstring
        if self._rowwise_data is not None:
            return self._rowwise_data.size(*args, **kwargs)
        return self._columnwise_data.size(*args, **kwargs)

    def view(self, shape: torch.Size):
        # pylint: disable=missing-function-docstring

        # Return input tensor if view not needed
        cur_shape = self.size()
        if shape is None or shape == cur_shape:
            return self

        # Canonicalize shape
        if not isinstance(shape, Iterable):
            shape = [shape]
        elif len(shape) == 1 and isinstance(shape[0], Iterable):
            shape = shape[0]
        if -1 in shape:
            shape = list(shape)
            d_inferred = -math.prod(cur_shape) // math.prod(shape)
            for i, d in enumerate(shape):
                if d == -1:
                    shape[i] = d_inferred
                    break
        if shape[-1] != cur_shape[-1]:
            raise RuntimeError(
                "MXFP8Tensor does not support reshaping inner dimension "
                f"(attempted to reshape dims={tuple(cur_shape)} to {tuple(shape)})"
            )

        # Construct new tensor
        cur_rowwise_data = self._rowwise_data
        cur_columnwise_data = self._columnwise_data
        new_rowwise_data = None
        new_columnwise_data = None
        if cur_rowwise_data is not None:
            new_rowwise_data = cur_rowwise_data.view(*shape)
        if cur_columnwise_data is not None:
            new_columnwise_data = cur_columnwise_data.view(*shape)

        return MXFP8TensorBase(
            rowwise_data=new_rowwise_data,
            rowwise_scale_inv=self._rowwise_scale_inv,
            columnwise_data=new_columnwise_data,
            columnwise_scale_inv=self._columnwise_scale_inv,
            fp8_dtype=self._fp8_dtype,
            quantizer=self._quantizer,
        )

    def __repr__(self):
        data_rowwise = self.dequantize()

        return (
            "MXFP8TensorBase("
            f"fp8_dtype={self._fp8_dtype}, "
            f"rowwise_scaled_data={data_rowwise}"
            f"rowwise_scale_inv={self._rowwise_scale_inv}, "
            ")"
        )

    def update_usage(
        self,
        rowwise_usage: Optional[bool] = None,
        columnwise_usage: Optional[bool] = None,
    ):
        """
        For MXFP8, columnwise scaled output is only produced by x2
        scaling kernels, so this function only disables usages.
        """

        # Default usage is based on available data
        if rowwise_usage is None:
            rowwise_usage = self._rowwise_data is not None
        if columnwise_usage is None:
            columnwise_usage = self._columnwise_data is not None

        # Update row-scaled data
        if rowwise_usage:
            if self._rowwise_data is None:
                raise RuntimeError(
                    "Requested row-wise usage, but MXFP8Tensor is missing row-scaled FP8 data"
                )
            if self._rowwise_scale_inv is None:
                raise RuntimeError(
                    "Requested row-wise usage, but MXFP8Tensor is missing row-scaled scale-inverses"
                )
        else:
            self._rowwise_data = None
            self._rowwise_scale_inv = None

        # Update column-scaled data
        if columnwise_usage:
            if self._columnwise_data is None:
                raise RuntimeError(
                    "Requested column-wise usage, but MXFP8Tensor is missing column-scaled FP8 data"
                )
            if self._columnwise_scale_inv is None:
                raise RuntimeError(
                    "Requested column-wise usage, "
                    "but MXFP8Tensor is missing column-scaled scale-inverses"
                )
        else:
            self._columnwise_data = None
            self._columnwise_scale_inv = None
