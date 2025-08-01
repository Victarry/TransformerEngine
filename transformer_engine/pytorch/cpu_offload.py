# Copyright (c) 2022-2025, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.

"""Functionality for CPU offloading of tensors saved for backward pass."""
from __future__ import annotations
from contextlib import nullcontext
from typing import Any, Dict, Optional

import torch

from transformer_engine.debug.pytorch.debug_state import TEDebugState
from .tensor.quantized_tensor import QuantizedTensorBase
from .tensor.float8_tensor import Float8Tensor

__all__ = ["get_cpu_offload_context"]

CPUOffloadEnabled = False


def mark_activation_offload(*tensors):
    """Set the type of the offloading needed for a tensor."""
    if TEDebugState.debug_enabled:
        raise RuntimeError("CPU offload is not supported in debug mode.")

    for tensor in tensors:
        if tensor is None:
            continue
        if type(tensor) in [torch.Tensor, torch.nn.Parameter]:
            tensor.activation_offloading = True
        else:
            data_tensors = tensor.get_data_tensors()
            for tensor in data_tensors:
                if tensor is not None:
                    tensor.activation_offloading = True
                    # This is a hack to force clear the tensor after it is offloaded.
                    # It is needed, because .*TensorBase classes are saved in the ctx,
                    # and they contain the reference to their data tensors.
                    tensor.needs_force_clear = True


def is_cpu_offload_enabled() -> bool:
    """Check if CPU offloading is currently enabled."""
    return CPUOffloadEnabled


class CpuOffloadSavedTensorHook:
    """Contex-manager that executes a pair of pack/unpack hooks for saved tensors.

    In this context, the ``on_save_for_backward`` method will be called every time
    a tensor is saved for backward (this includes intermediary results saved using
    :func:`~torch.autograd.function._ContextMethodMixin.save_for_backward` but
    also those recorded by a PyTorch-defined operation).

    The ``on_get_saved_tensors`` method will be called when the backward function
    of this op attempts to retrieve the saved tensor from context (this includes
    :func: `torch.Tensor.backward()` or :func: `torch.autograd.grad()`. It takes the
    as input the return value of the ``on_save_for_backward``, and is meant to return
    an identical copy of the tensor being saved by ``on_save_for_backward`` in terms of
    size, device and element values.

    Example:

        >>> import torch
        >>> from typing import Any
        >>>
        >>> class DummyHook(CpuOffloadSavedTensorHook):
        ...
        ...     def on_save_for_backward(self, tensor: torch.Tensor) -> Any:
        ...         logging.info("On save", tensor)
        ...         return (tensor,)
        ...
        ...     def on_get_saved_tensor(self, saved_state: Any) -> torch.Tensor:
        ...         logging.info("On get", saved_state)
        ...         tensor, = saved_state
        ...         return tensor
        ...
        >>> a = torch.ones(5, requires_grad=True)
        >>> b = torch.ones(5, requires_grad=True) * 2
        >>> with DummyHook():
        ...     y = a * b
        ...
        On save tensor([1., 1., 1., 1., 1.], requires_grad=True)
        On save tensor([2., 2., 2., 2., 2.], grad_fn=<MulBackward0>)
        >>> y.sum().backward()
        On get (tensor([1., 1., 1., 1., 1.], requires_grad=True),)
        On get (tensor([2., 2., 2., 2., 2.], grad_fn=<MulBackward0>),)

    """

    def __init__(self) -> None:
        self.inside_context = False

    def __enter__(self):
        global CPUOffloadEnabled
        CPUOffloadEnabled = True

        self.inside_context = True
        torch._C._autograd._push_saved_tensors_default_hooks(
            self.on_save_for_backward, self.on_get_saved_tensor
        )

    def __exit__(self, *args: Any):
        global CPUOffloadEnabled
        CPUOffloadEnabled = False

        self.inside_context = False
        torch._C._autograd._pop_saved_tensors_default_hooks()

    def on_save_for_backward(self, tensor: torch.Tensor) -> Any:
        """On save for backward."""
        raise NotImplementedError(
            "`on_save_for_backward: Callable[[torch.Tensor], Any]`"
            "is not implemented in CpuOffloadHook class. Inherit "
            "this class and implement your custom hooks"
        )

    def on_get_saved_tensor(self, saved_state: Any) -> torch.Tensor:
        """On get saved tensor."""
        raise NotImplementedError(
            "`on_get_saved_tensors: Callable[[Any], torch.Tensor]`"
            "is not implemented in CpuOffloadHook class. Inherit "
            "this class and implement your custom hooks"
        )


class CpuOffloadHookWithOffloadHandler(CpuOffloadSavedTensorHook):
    """Context-manager that offloads/recovers tensors through an offload hander.

    The hook just offloads/recovers the tensor object to the handler through `tensor_push`
    and `tensor_pop` interface. How the offload-handler manages the offloading, recovering
    or prefetching timing is transparent to this hook.
    """

    def __init__(
        self,
        offload_handler: OffloadHandler,
        handler_extra_kwargs: Optional[Dict[str, Any]] = None,
        debug: bool = False,
    ) -> None:
        if handler_extra_kwargs is None:
            handler_extra_kwargs = {}
        self.debug: bool = debug
        self.offload_handler: OffloadHandler = offload_handler
        self.handler_extra_kwargs: Dict[str, Any] = handler_extra_kwargs
        super().__init__()

    def on_save_for_backward(self, tensor: torch.Tensor) -> Any:
        retrieve_identifier = self.offload_handler.tensor_push(tensor, **self.handler_extra_kwargs)
        return retrieve_identifier

    def on_get_saved_tensor(self, saved_state: Any) -> torch.Tensor:
        tensor = self.offload_handler.tensor_pop(saved_state, **self.handler_extra_kwargs)
        return tensor


class OffloadHandler:
    """A base class for CPU offload-handler."""

    def __init__(self) -> None:
        pass

    def tensor_push(self, tensor: torch.Tensor, **kwargs) -> Any:
        """Tensor push."""
        raise NotImplementedError(
            "`tensor_push is not implented in OffloadHandler class. "
            "Inherit this class and implement your custom tensor_push."
        )

    def tensor_pop(self, tensor_tag: Any, **kwargs):
        """Tensor pop."""
        raise NotImplementedError(
            "`tensor_pop is not implented in OffloadHandler class. "
            "Inherit this class and implement your custom tensor_pop."
        )


class GroupCommitFunction(torch.autograd.Function):
    """this is a dummy op with output identical to input.
    However, it is necessary for marking a timepoint for offload handler to
    accomplish all synchronizations. Implementing it as a function is necessary
    because we need to actions in both forward and backward.
    """

    @staticmethod
    def forward(ctx, tensor, cpu_offload_handler):
        # pylint: disable=missing-function-docstring
        cpu_offload_handler.on_group_commit_forward()
        ctx.cpu_offload_handler = cpu_offload_handler
        # return the identical tensor
        return tensor

    @staticmethod
    def backward(ctx, grad_output):
        # pylint: disable=missing-function-docstring
        cpu_offload_handler = ctx.cpu_offload_handler
        cpu_offload_handler.on_group_commit_backward()
        return grad_output, None


group_prefetch_offload_commit = GroupCommitFunction.apply


class SynchronizedGroupOffloadHandler(OffloadHandler):
    """Offload Handler that offloads/reloads in a synchronized way.
    The device-to-host and host-to-device copying happen in the same stream
    as the computation kernels, thus the copying will block computation.
    """

    def __init__(
        self, num_offload_group, tensor_need_offloading_checker=(lambda _: True), debug=False
    ) -> None:
        super().__init__()

        self.num_offload_group = num_offload_group
        self.tensor_need_offloading_checker = tensor_need_offloading_checker
        self.debug = debug

        self.groupid_reset()

    def groupid_reset(self):
        """Groupid reset."""
        # Data structures to label saved tensors and book-keep their cpu copies.
        # Currently, on push, create a new cpu tensor and copies; on pop, copies
        # the tensor back to gpu and deletes the cpu tensor.
        # These will increment whenever `group_commit()` is invoked
        self.current_group, self.tensor_count_current_group = (0, 0)
        self.torch_tensor_count = 0
        self.tensor_tag_to_state = {}

    def on_group_commit_forward(self):
        """On group commit forward."""
        # finishing up with updating current group and tensor count
        self.current_group += 1  # increment
        self.tensor_count_current_group = 0  # reset

    def on_group_commit_backward(self):
        """On group commit backward."""
        self.current_group -= 1
        assert self.current_group >= 0

    @staticmethod
    def offload(src_tensor, pin_memory=True):
        """Offload."""

        cpu_backup = torch.empty(
            src_tensor.size(),
            dtype=src_tensor.dtype,
            layout=src_tensor.layout,
            device="cpu",
            pin_memory=pin_memory,
        )

        cpu_backup.copy_(src_tensor, non_blocking=pin_memory)
        state = (src_tensor.device, cpu_backup)
        return state

    @staticmethod
    def reload(state, non_blocking=None, copy_buffer=None):
        """Reload."""
        dev, cpu_backup = state
        if non_blocking is None:
            non_blocking = cpu_backup.is_pinned()

        if copy_buffer is None:
            return cpu_backup.to(dev, non_blocking=non_blocking)

        assert cpu_backup.size() == copy_buffer.size(), "Can't copy two buffers of different sizes!"

        copy_buffer.copy_(cpu_backup, non_blocking=non_blocking)

        return copy_buffer

    def tensor_push(self, tensor: torch.Tensor, **kwargs):
        """Tensor push."""
        # obtain a unique tensor tag
        tensor_tag = (self.current_group, self.tensor_count_current_group)
        self.tensor_count_current_group += 1
        assert tensor_tag not in self.tensor_tag_to_state
        if self.current_group < self.num_offload_group and self.tensor_need_offloading_checker(
            tensor
        ):
            state = SynchronizedGroupOffloadHandler.offload(tensor)
            self.tensor_tag_to_state[tensor_tag] = state
        else:
            # will be offloaded together after group commit
            self.tensor_tag_to_state[tensor_tag] = tensor

        return tensor_tag

    def tensor_pop(self, tensor_tag, **kwargs):
        """Tensor pop."""
        assert tensor_tag in self.tensor_tag_to_state
        state = self.tensor_tag_to_state.pop(tensor_tag)
        if isinstance(state, tuple):
            tensor = SynchronizedGroupOffloadHandler.reload(state)
        else:
            tensor = state
        return tensor


class AsyncDoubleBufferGroupOffloadHandler(SynchronizedGroupOffloadHandler):
    """Compared to synchronize, this uses more memory because of the buffer but
    achieves better performance due to the overlapping. D2h and h2d copying are
    completely hidden behind computation if computation time of a layer is longer
    than host-device communication time. Bulk offloading with delay and bulk reloading
    with prefetch are implemented."""

    def __init__(
        self,
        num_offload_group,  # must be <= actual number of groups (number of commits)
        num_model_group,
        tensor_need_offloading_checker=(lambda t: True),
        double_buffering=False,
        debug=False,
    ) -> None:
        super().__init__(
            num_offload_group=num_offload_group,
            tensor_need_offloading_checker=tensor_need_offloading_checker,
            debug=debug,
        )
        # Number of layers in the model
        self.num_layers = num_model_group
        # Data Structure to maintain reference to activation tensors
        self.tensor_tag_to_buf = {}
        # Data structure to hold the FP8/MXFP8 tensor objects
        self.fp8_tensor_object_map = {}
        self.float8_transpose_cache_valid = {}
        self.dereferencing_list = []
        # Tracking the number of layers offloaded
        self.offloaded_group_count = 0
        # Core data structure that decides the window for offloading
        self.layer_window_map = {}

        # Data structures fo double buffered reloading
        self.double_buffering = double_buffering
        self.reload_double_buffer = [[], []]
        self.double_buffer_created = False

        # Logic to make offloading load balance across computation
        # for optimal CPU/GPU interconnect usage
        constant = 0
        for i in range(self.num_offload_group):
            self.layer_window_map[i] = ((self.num_layers // self.num_offload_group) * (i + 1)) - 1
            if i < (self.num_layers % self.num_offload_group):
                self.layer_window_map[i] += i + 1
                constant = i + 1
            else:
                self.layer_window_map[i] += constant

        # allocate streams and events for synchronization
        self.d2h_stream = torch.cuda.Stream()
        self.h2d_stream = torch.cuda.Stream()

    def tensor_push(self, tensor: torch.Tensor, **kwargs) -> Any:

        torch_stray_tensor = isinstance(
            tensor,
            (
                torch._subclasses.fake_tensor.FakeTensor,
                torch._subclasses.functional_tensor.FunctionalTensor,
            ),
        )

        is_quantized_tensor = isinstance(tensor, QuantizedTensorBase)

        if not torch_stray_tensor:

            # obtain a unique tensor tag
            tensor_tag = (self.current_group, self.tensor_count_current_group)
            self.tensor_count_current_group += 1

            assert tensor_tag not in self.tensor_tag_to_state

            if is_quantized_tensor:
                tensor_list, _ = tensor.prepare_for_saving()

                self.tensor_tag_to_state[tensor_tag] = []
                self.tensor_tag_to_buf[tensor_tag] = []

                # Added support for de-duplicating FP8 param tensors
                for _, value in self.fp8_tensor_object_map.items():
                    if tensor is value:
                        self.dereferencing_list.append(tensor_tag)
                        break

                self.fp8_tensor_object_map[tensor_tag] = tensor
                if isinstance(tensor, Float8Tensor):
                    self.float8_transpose_cache_valid[tensor_tag] = getattr(
                        tensor, "_transpose_invalid"
                    )
            else:
                tensor_list = [tensor]

            for t in tensor_list:
                if is_quantized_tensor:
                    self.tensor_tag_to_state[tensor_tag].append(t)
                else:
                    self.tensor_tag_to_state[tensor_tag] = t

                if (
                    self.current_group < self.num_offload_group
                    and self.tensor_need_offloading_checker(t)
                ):
                    if is_quantized_tensor:
                        self.tensor_tag_to_buf[tensor_tag].append(t)
                        # Need to clear the internal data reference for the quantized tensors
                        tensor.clear()
                    else:
                        self.tensor_tag_to_buf[tensor_tag] = t
        else:
            tensor_tag = (-1, self.torch_tensor_count)
            self.torch_tensor_count += 1
            self.tensor_tag_to_state[tensor_tag] = tensor

        return tensor_tag

    def tensor_pop(self, tensor_tag, **kwargs):
        """Tensor pop."""
        assert tensor_tag in self.tensor_tag_to_state
        tensor = self.tensor_tag_to_state.pop(tensor_tag)

        # Handling the quantized tensor case specially here
        if isinstance(tensor, list):
            # If it's a duplicated tensor, we don't need to locally
            # write back a tensor as it would already be written
            if tensor_tag in self.dereferencing_list:
                self.dereferencing_list.remove(tensor_tag)
            else:
                self.fp8_tensor_object_map[tensor_tag].restore_from_saved(tensor)
            tensor = self.fp8_tensor_object_map.pop(tensor_tag)

        if self.double_buffering:
            tensor._do_not_clear = True

        self.tensor_tag_to_buf.pop(tensor_tag, None)
        # the tensor should have been copied back in on_group_commit_backward()
        # which invokes bulk_reload_group.
        assert not isinstance(tensor, tuple)
        return tensor

    def bulk_offload_group(self, group_to_offload):
        """Bulk offload group."""
        with torch.cuda.stream(self.d2h_stream):
            for tensor_tag, state in self.tensor_tag_to_state.items():
                group_id, _ = tensor_tag
                if group_id == group_to_offload:
                    assert not isinstance(state, tuple)

                    is_quantized_tensor = isinstance(state, list)

                    if is_quantized_tensor:
                        tensor_list = state
                        self.tensor_tag_to_state[tensor_tag] = []
                    else:
                        tensor_list = [state]

                    for tensor_on_device in tensor_list:
                        # `tensor_offloaded` is a hacky way of dealing with columnwise-only
                        # quantized tensors for CPU offloading. The complication is due to
                        # the `rowwise_data` being `None`. The offloading checker incorrectly
                        # returns `False` and the entire `state` ([None, columnwise_tensor])
                        # is added to the tensor tag state dict. A better design would change
                        # how quantized tensors are kept track of in the offload handler.
                        # Currently at every stage it is ensured that a quantized tensor is a
                        # list whereas a non-quantized tensor is standalone object, which is
                        # not good! TODO(@sanandaraj5597)
                        tensor_offloaded = False
                        # if offload, return the reference to cpu copy
                        if self.tensor_need_offloading_checker(tensor_on_device):
                            tensor_offloaded = True
                            state = SynchronizedGroupOffloadHandler.offload(tensor_on_device)
                        if is_quantized_tensor:
                            if tensor_offloaded:
                                self.tensor_tag_to_state[tensor_tag].append(state)
                            else:
                                self.tensor_tag_to_state[tensor_tag].append(tensor_on_device)
                        else:
                            self.tensor_tag_to_state[tensor_tag] = state

    def synchronize_on_group_commit_forward(self, current_group):
        """Synchronize on group commit forward."""

        # For the first group, kickstart the offload after we have
        # the first compute completion
        if current_group == 0:
            self.d2h_stream.wait_stream(torch.cuda.current_stream())

            if not self.double_buffer_created:
                # Creating the first copy of double buffer for tensors that are offloaded
                for tensor_tag, buf in self.tensor_tag_to_buf.items():
                    if isinstance(buf, list):
                        for b in buf:
                            self.reload_double_buffer[0].append(
                                torch.empty_like(b) if self.double_buffering else None
                            )
                    else:
                        self.reload_double_buffer[0].append(
                            torch.empty_like(buf) if self.double_buffering else None
                        )

            self.bulk_offload_group(current_group)

        # Window map data structure helps us synchronize based on number
        # of layers offloaded
        if self.layer_window_map[self.offloaded_group_count] == current_group:

            # Stream synchronization both ways
            self.d2h_stream.wait_stream(torch.cuda.current_stream())
            torch.cuda.current_stream().wait_stream(self.d2h_stream)

            # Time to free the activation memory after usage
            for tensor_tag, tensor_buf in self.tensor_tag_to_buf.items():
                if tensor_tag[0] == self.offloaded_group_count:
                    if hasattr(tensor_buf, "needs_force_clear"):
                        # Need to clear activation tensor - sometimes references persist in the code.
                        # This is the case for example with the Float8TensorBase class,
                        # which is saved directly inside the ctx while its internal tensors are
                        # saved inside save_for_backward.
                        tensor_buf.data = torch.Tensor()
                    # Release the pointer to the tensor
                    self.tensor_tag_to_buf[tensor_tag] = None

            # Time to offload the next group
            if self.offloaded_group_count < (self.num_offload_group - 1):
                self.bulk_offload_group(self.offloaded_group_count + 1)

            # Increment the offload group count to keep track
            self.offloaded_group_count += 1

        if not self.double_buffer_created:
            # Creating second copy of double buffer for tensors that are offloaded
            if current_group == (self.num_layers - 1):
                for buf in self.reload_double_buffer[0]:
                    self.reload_double_buffer[1].append(
                        torch.empty_like(buf) if self.double_buffering else None
                    )
                self.double_buffer_created = True

    def on_group_commit_forward(self):
        """This function will cause host device synchronization"""
        # handle synchronization events
        self.synchronize_on_group_commit_forward(self.current_group)

        super().on_group_commit_forward()

    def bulk_reload_group(self, group_to_reload):
        """Bulk reload group."""
        assert group_to_reload < self.num_offload_group

        buffer_idx = 0
        double_buffer_idx = group_to_reload % 2

        with torch.cuda.stream(self.h2d_stream):
            # move back tensors
            for tensor_label, state in self.tensor_tag_to_state.items():
                group_id, _ = tensor_label
                if group_id == group_to_reload:
                    if self.double_buffering:
                        reload_buffer = self.reload_double_buffer[double_buffer_idx][buffer_idx]
                    else:
                        reload_buffer = None

                    if isinstance(state, tuple):
                        recovered_tensor = SynchronizedGroupOffloadHandler.reload(
                            state, True, reload_buffer
                        )
                        buffer_idx = buffer_idx + 1
                        self.tensor_tag_to_state[tensor_label] = recovered_tensor
                    elif isinstance(state, list):
                        tensor_list = []
                        for state_tuple in state:
                            if self.double_buffering:
                                reload_buffer = self.reload_double_buffer[double_buffer_idx][
                                    buffer_idx
                                ]
                            else:
                                reload_buffer = None

                            if isinstance(state_tuple, tuple):
                                tensor_list.append(
                                    SynchronizedGroupOffloadHandler.reload(
                                        state_tuple,
                                        True,
                                        reload_buffer,
                                    )
                                )
                                buffer_idx = buffer_idx + 1
                            else:
                                tensor_list.append(state_tuple)

                        # No need to write back the duplicated tensor againn
                        # to the same location, this check ensures that
                        if tensor_label in self.dereferencing_list:
                            self.dereferencing_list.remove(tensor_label)
                        else:
                            _ = self.fp8_tensor_object_map[tensor_label].restore_from_saved(
                                tensor_list
                            )

                        if isinstance(self.fp8_tensor_object_map[tensor_label], Float8Tensor):
                            self.fp8_tensor_object_map[tensor_label]._transpose_invalid = (
                                self.float8_transpose_cache_valid.pop(tensor_label)
                            )

                        self.tensor_tag_to_state[tensor_label] = self.fp8_tensor_object_map.pop(
                            tensor_label
                        )

    def on_group_commit_backward(self):
        # first decrement the current group.
        # after last commit in forward, the group will +1; in backward it -1.
        # Finally it should be decremented to 0.
        self.current_group -= 1
        assert self.current_group >= 0

        # Layer window data structure helps us to reload at right times
        if self.layer_window_map[self.offloaded_group_count - 1] == self.current_group:

            # Stream synchronization both ways
            self.h2d_stream.wait_stream(torch.cuda.current_stream())
            torch.cuda.current_stream().wait_stream(self.h2d_stream)

            # Time to reload the next group
            self.bulk_reload_group(self.offloaded_group_count - 1)

            # Decrease the offloading group counter
            self.offloaded_group_count -= 1 if self.offloaded_group_count > 1 else 0

        # Last group computation needs to wait till all the reloads complete
        if self.current_group == 0:
            torch.cuda.current_stream().wait_stream(self.h2d_stream)
            self.offloaded_group_count = 0


def get_cpu_offload_context(
    enabled: bool = False,
    num_layers: int = 1,
    model_layers: int = 1,
    offload_activations: bool = True,
    offload_weights: bool = False,
    double_buffering: bool = False,
):
    """
    This function returns the CPU Offload context and the synchronizer function that needs to be
    used after every transformer layer. Returns `nullcontext()` if offloading is not enabled.

    Usage:

    .. code-block:: python

        cpu_offload_context, cpu_offload_synchronizer = get_cpu_offload_context(enabled=True)

        with cpu_offload_context:
            te_layer.forward(inp_tensor)
        cpu_offload_synchronizer()

    Parameters
    ----------
    enabled: bool, default = `False`
             When set to True, CPU Offloading functionality is enabled.
    num_layers: int, default = 1
                Determines the number of transformer layers
                you want to offload activations/weights for.
    model_layers: int, default = 1
                  Number of layers in the model that will be used under this context.
    offload_activations: bool, default = `True`
                         When set to `True`, offloads the activations for the TE layer.
    offload_weights: bool, default = `True`
                     When set to `True`, offloads the weights for the TE layer.
    double_buffering: bool, default = `False`
                      When set to `True`, uses double buffering for offloading.

    """

    if not offload_weights and not offload_activations:
        raise ValueError(
            "CPU Offloading is enabled while it is not "
            "mentioned what to offload (weights/activations)"
        )

    if offload_weights:
        import warnings

        warnings.warn(
            "Offloading weights is deprecated. Using offload_weights=True does not have any"
            " effect.",
            DeprecationWarning,
        )

        # Weights offloading is deprecated but we maintain backward compatibility by doing nothing.
        if not offload_activations:
            return nullcontext(), lambda x: x

    def tensor_need_offloading_checker_activations(tensor):
        return hasattr(tensor, "activation_offloading")

    tensor_need_offloading_checker = tensor_need_offloading_checker_activations

    cpu_offload_handler = AsyncDoubleBufferGroupOffloadHandler(
        num_offload_group=num_layers,
        num_model_group=model_layers,
        tensor_need_offloading_checker=tensor_need_offloading_checker,
        double_buffering=double_buffering,
    )

    def group_prefetch_offload_commit_async(tensor):
        return group_prefetch_offload_commit(tensor, cpu_offload_handler)

    if enabled:
        return (
            CpuOffloadHookWithOffloadHandler(offload_handler=cpu_offload_handler),
            group_prefetch_offload_commit_async,
        )
    return nullcontext(), group_prefetch_offload_commit_async
