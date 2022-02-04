from typing import List, Dict, Any, Tuple

import torch
import torch.nn as nn
import torch.utils.dlpack
from torch import Tensor
from torch.fx import immutable_collections
from torch.utils import _pytree as pytree

from oslo.pytorch._C import CompileCache
from oslo.pytorch.kernel_fusion.utils import _stateless
from oslo.pytorch.kernel_fusion.utils.decompositions import register_decomposition
from oslo.pytorch.kernel_fusion.utils.partitioners import default_partition
from oslo.pytorch.kernel_fusion.utils.python_key import make_fx

pytree._register_pytree_node(
    immutable_collections.immutable_list,
    lambda x: (list(x), None),
    lambda x, c: immutable_collections.immutable_list(x),
)
pytree._register_pytree_node(
    immutable_collections.immutable_dict,
    lambda x: (list(x.values()), list(x.keys())),
    lambda x, c: immutable_collections.immutable_dict(
        {key: value for key, value in zip(c, x)}
    ),
)

Context = Any


def _dict_flatten(d: Dict[Any, Any]) -> Tuple[List[Any], Context]:
    keys = list(sorted(d.keys()))
    values = [d[key] for key in keys]
    return values, keys


def _dict_unflatten(values: List[Any], context: Context) -> Dict[Any, Any]:
    return {key: value for key, value in zip(context, values)}


pytree._register_pytree_node(dict, _dict_flatten, _dict_unflatten)

aten = torch.ops.aten


def create_joint_forward_backward(fn):
    def joint_forward_backward(
        primals: List[Any], tangents: List[Any]
    ) -> Tuple[List[Any], List[Any]]:
        # Call the forward pass
        outs = fn(*primals)
        # Get the inputs that need gradients
        grad_primals = []
        inputs_needs_grads = []
        for p in primals:
            is_grad_tensor = isinstance(p, Tensor) and p.requires_grad
            inputs_needs_grads.append(is_grad_tensor)
            if is_grad_tensor:
                grad_primals.append(p)

        # Get the outputs that need gradients
        assert len(tangents) == len(outs)
        needed_outs = []
        needed_tangents = []
        for out, tangent in zip(outs, tangents):
            if isinstance(out, Tensor) and out.requires_grad:
                needed_outs.append(out)
                needed_tangents.append(tangent)
        backward_out = []
        # Call the backwards pass
        if grad_primals:
            backward_out = torch.autograd.grad(
                needed_outs,
                grad_primals,
                grad_outputs=needed_tangents,
                allow_unused=True,
            )
        backward_out_iter = iter(backward_out)
        return outs, [
            next(backward_out_iter) if i else None for i in inputs_needs_grads
        ]

    return joint_forward_backward


def normalize_as_list(x):
    if isinstance(x, tuple):
        return list(x)
    elif isinstance(x, list):
        return x
    return [x]


aot_autograd_decompositions = {}


@register_decomposition(aten.rsub, aot_autograd_decompositions)
def rsub(a, b, alpha=1):
    return -aten.sub(a, b)


def create_aot_autograd_function(
    flat_fn, fw_compiler, bw_compiler, partition_fn, decompositions, grad_state
):
    joint_forward_backward = create_joint_forward_backward(flat_fn)

    compiled_fw = None
    compiled_bw = None
    num_outs = None

    class CompiledFunction(torch.autograd.Function):
        @staticmethod
        def forward(ctx, *flat_tensor_args):
            nonlocal compiled_fw, compiled_bw, num_outs
            if compiled_fw is None:
                with torch.set_grad_enabled(grad_state):
                    out = flat_fn(*flat_tensor_args)
                out = pytree.tree_map(
                    lambda x: x.detach() if isinstance(x, Tensor) else x, out
                )

                if isinstance(out, (list, tuple)):
                    num_outs = len(out)
                else:
                    num_outs = 1

                joint_inputs = (flat_tensor_args, out)
                aot_decompositions = {**aot_autograd_decompositions, **decompositions}
                with torch.set_grad_enabled(grad_state):
                    fx_g = make_fx(joint_forward_backward, aot_decompositions)(
                        *joint_inputs
                    )
                fw_module, bw_module = partition_fn(fx_g, joint_inputs)
                # print(fw_module.code, bw_module.code)

                compiled_fw = fw_compiler(fw_module, flat_tensor_args)
                fw_outs = normalize_as_list(compiled_fw(*flat_tensor_args))

                bw_args = fw_outs[num_outs:] + fw_outs[0:num_outs]
                compiled_bw = bw_compiler(bw_module, bw_args)
            else:
                fw_outs = normalize_as_list(compiled_fw(*flat_tensor_args))
            ctx.save_for_backward(*fw_outs[num_outs:])
            return tuple(fw_outs[0:num_outs])

        @staticmethod
        def backward(ctx, *flat_args):
            # hmm... this doesn't feel right. todo
            # contiguous_args = [t.contiguous() for t in flat_args]
            contiguous_args = [t for t in flat_args]
            out = normalize_as_list(compiled_bw(*ctx.saved_tensors, *contiguous_args))
            return tuple(out)

    return CompiledFunction


class _CompileCache(CompileCache):
    pass


compile_cache = None


# Inspired by autodidax (thanks!)
class PytreeThunk:
    spec = None
    # These are some kinda dumb microoptimizations that save about 3-4 us of overhead.
    is_simple = (
        None  # if the output spec is a tuple/list, we won't bother unflattening it.
    )
    is_really_simple = None  # if the output spec is a LeafSpec

    def set(self, spec):
        assert self.spec is None or self.spec == spec
        self.spec = spec
        if type(self.spec) in [tuple, list] and all(
            [isinstance(i, pytree.LeafSpec) for i in spec.children_specs]
        ):
            self.is_simple = True
        if isinstance(self.spec, pytree.LeafSpec):
            self.is_really_simple = True

    def unflatten(self, x):
        if self.is_really_simple:
            return x[0]
        if self.is_simple:
            return x
        return pytree.tree_unflatten(x, self.spec)


def filter_tensor_and_static_args(args, static_argnums):
    """
    Separate out the tensor and static args. Also, for the static args, store
    the hash.
    """
    tensor_args = []
    static_args = []
    static_args_hashed = []
    for idx, arg in enumerate(args):
        if idx not in static_argnums:
            tensor_args.append(arg)
        else:
            static_args.append(arg)
            static_args_hashed.append(arg.__hash__())
    return tensor_args, static_args, static_args_hashed


def rearrange(tensor_args, static_args, static_argnums):
    """
    Generate the args as per the original spec. static_argnums is sorted.
    """
    tensor_index = 0
    static_index = 0
    index = 0
    args = []
    assert len(static_args) == len(static_argnums)
    while tensor_index < len(tensor_args) and static_index < len(static_args):
        if index == static_argnums[static_index]:
            args.append(static_args[static_index])
            static_index += 1
        else:
            args.append(tensor_args[tensor_index])
            tensor_index += 1

    while tensor_index < len(tensor_args):
        args.append(tensor_args[tensor_index])
        tensor_index += 1

    while static_index < len(static_args):
        args.append(static_args[static_index])
        static_index += 1

    return args


def aot_function(
    fn,
    fw_compiler,
    bw_compiler=None,
    partition_fn=default_partition,
    decompositions={},
    hasher_type="StaticShapeHasher",
    static_argnums=None,
):
    global compile_cache
    if compile_cache is None:
        compile_cache = CompileCache()
    if bw_compiler is None:
        bw_compiler = fw_compiler
    cached_res = None

    fn_id = id(fn)
    fw_compiler_id = id(fw_compiler)
    bw_compiler_id = id(bw_compiler)

    if isinstance(static_argnums, int):
        static_argnums = [static_argnums]
    elif static_argnums is not None and len(static_argnums) == 0:
        static_argnums = None
    elif static_argnums is not None:
        static_argnums = list(static_argnums)
        static_argnums.sort()

    def returned_function(*args, **kwargs):
        global compile_cache
        nonlocal cached_res

        tensor_args = args
        static_args = []
        static_args_hashed = []
        if static_argnums is not None:
            (
                tensor_args,
                static_args,
                static_args_hashed,
            ) = filter_tensor_and_static_args(args, static_argnums)

        # Now flatten the tensor args
        flat_tensor_args, _ = pytree.tree_flatten((tensor_args, kwargs))

        # Check if the fn is already compiled
        num_tensor_args = len(flat_tensor_args)
        flat_args_for_cache = flat_tensor_args + static_args_hashed
        cached_res = compile_cache.at(
            fn_id,
            fw_compiler_id,
            bw_compiler_id,
            num_tensor_args,
            hasher_type,
            *flat_args_for_cache,
        )

        # Compile the function and save it in the cache
        if cached_res is None:
            # Save the args_spec for flat_tensor_args to unflatten while tracing
            _, tensor_args_spec = pytree.tree_flatten((tensor_args, kwargs))
            out_spec = PytreeThunk()

            def flat_fn(*flat_tensor_args):
                # The input are flattened tensor args. Prepare the args in the
                # order that original function expects. Add static args as well.
                # They will appear as tensor constants in the traced graph.
                nonlocal out_spec, static_args

                tensor_args, kwargs = pytree.tree_unflatten(
                    flat_tensor_args, tensor_args_spec
                )
                if static_argnums is None:
                    args = tensor_args
                else:
                    args = rearrange(tensor_args, static_args, static_argnums)

                tree_out = fn(*args, **kwargs)
                flat_out, spec = pytree.tree_flatten(tree_out)
                out_spec.set(spec)
                return flat_out

            compiled_fn = create_aot_autograd_function(
                flat_fn,
                fw_compiler,
                bw_compiler,
                partition_fn,
                decompositions,
                grad_state=torch.is_grad_enabled(),
            ).apply
            cached_res = (compiled_fn, out_spec)

            # Save the compiled_fn in the cache
            compile_cache.insert(
                fn_id,
                fw_compiler_id,
                bw_compiler_id,
                num_tensor_args,
                hasher_type,
                cached_res,
                *flat_args_for_cache,
            )

        cached_fn, out_spec = cached_res
        out = cached_fn(*flat_tensor_args)
        return out_spec.unflatten(out)

    return returned_function


def aot_module(mod, *args, **kwargs):
    def functional_call(named_params, named_buffers, *args, **kwargs):
        params_and_buffers = {**named_params, **named_buffers}
        return _stateless.functional_call(mod, params_and_buffers, args, kwargs)

    compiled_f = aot_function(functional_call, *args, **kwargs)

    def forward(*args, **kwargs):
        return compiled_f(
            dict(mod.named_parameters()),
            dict(mod.named_buffers()),
            *args,
            **kwargs,
        )

    mod.forward = forward

    return mod


compiled_function = aot_function
compiled_module = aot_module
