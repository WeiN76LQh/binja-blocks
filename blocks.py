# Copyright (c) 2024 Daniel Roethlisberger
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import binaryninja as binja

import sys
import traceback

from . import shinobi
from . import objctypes


def is_valid(bv, arg=None):
    return bv.arch.name in (
        'aarch64',
        'x86_64',
        #'armv7',
        #'x86',
    )


_TYPE_ID_SOURCE = "binja-blocks"


_OBJC_TYPE_SOURCE = """
struct objc_class {
};

typedef struct objc_class* Class;

struct objc_object {
    Class isa;
};

typedef struct objc_object* id;
"""

_LIBCLOSURE_TYPE_SOURCE = """
enum BLOCK_LITERAL_FLAGS : uint32_t {
    BLOCK_DEALLOCATING              = 0x0001U,      // runtime
    BLOCK_REFCOUNT_MASK             = 0xfffeU,      // runtime
    BLOCK_INLINE_LAYOUT_STRING      = 1U << 21,     // compiler
    BLOCK_SMALL_DESCRIPTOR          = 1U << 22,     // compiler
    BLOCK_IS_NOESCAPE               = 1U << 23,     // compiler
    BLOCK_NEEDS_FREE                = 1U << 24,     // runtime
    BLOCK_HAS_COPY_DISPOSE          = 1U << 25,     // compiler
    BLOCK_HAS_CTOR                  = 1U << 26,     // compiler
    BLOCK_IS_GC                     = 1U << 27,     // runtime
    BLOCK_IS_GLOBAL                 = 1U << 28,     // compiler
    BLOCK_USE_STRET                 = 1U << 29,     // compiler
    BLOCK_HAS_SIGNATURE             = 1U << 30,     // compiler
    BLOCK_HAS_EXTENDED_LAYOUT       = 1U << 31,     // compiler
};

enum BLOCK_BYREF_FLAGS : uint32_t {
    BLOCK_BYREF_DEALLOCATING        = 0x0001U,      // runtime
    BLOCK_BYREF_REFCOUNT_MASK       = 0xfffeU,      // runtime
    BLOCK_BYREF_NEEDS_FREE          = 1U << 24,     // runtime
    BLOCK_BYREF_HAS_COPY_DISPOSE    = 1U << 25,     // compiler
    BLOCK_BYREF_IS_GC               = 1U << 27,     // runtime
    BLOCK_BYREF_LAYOUT_MASK         = 7U << 28,     // compiler
    BLOCK_BYREF_LAYOUT_EXTENDED     = 1U << 28,     // compiler
    BLOCK_BYREF_LAYOUT_NON_OBJECT   = 2U << 28,     // compiler
    BLOCK_BYREF_LAYOUT_STRONG       = 3U << 28,     // compiler
    BLOCK_BYREF_LAYOUT_WEAK         = 4U << 28,     // compiler
    BLOCK_BYREF_LAYOUT_UNRETAINED   = 5U << 28,     // compiler
};

typedef void(*BlockCopyFunction)(void *, const void *);
typedef void(*BlockDisposeFunction)(const void *);
typedef void(*BlockInvokeFunction)(void *, ...);

struct Block_byref_1 {
    Class isa;
    struct Block_byref_1 *forwarding;
    volatile uint32_t flags;
    uint32_t size;
};

typedef void(*BlockByrefKeepFunction)(struct Block_byref*, struct Block_byref*);
typedef void(*BlockByrefDestroyFunction)(struct Block_byref *);

struct Block_byref_2 {
    BlockByrefKeepFunction byref_keep;
    BlockByrefDestroyFunction byref_destroy;
};

struct Block_byref_3 {
    const char *layout;
};

struct Block_descriptor_1 {
    uint64_t reserved;
    uint64_t size;
};

struct Block_descriptor_2 {
    BlockCopyFunction copy;
    BlockDisposeFunction dispose;
};

struct Block_descriptor_3 {
    const char *signature;
    const uint8_t *layout;
};

struct Block_literal {
    Class isa;
    volatile uint32_t flags;
    uint32_t reserved;
    BlockInvokeFunction invoke;
    struct Block_descriptor_1 *descriptor;
    // ... imported variables
};
"""

BLOCK_HAS_EXTENDED_LAYOUT       = 0x80000000
BLOCK_HAS_SIGNATURE             = 0x40000000
BLOCK_IS_GLOBAL                 = 0x10000000
BLOCK_HAS_COPY_DISPOSE          = 0x02000000

BLOCK_BYREF_HAS_COPY_DISPOSE    = 0x02000000
BLOCK_BYREF_LAYOUT_MASK         = 0x70000000
BLOCK_BYREF_LAYOUT_EXTENDED     = 0x10000000
BLOCK_BYREF_LAYOUT_NON_OBJECT   = 0x20000000
BLOCK_BYREF_LAYOUT_STRONG       = 0x30000000
BLOCK_BYREF_LAYOUT_WEAK         = 0x40000000
BLOCK_BYREF_LAYOUT_UNRETAINED   = 0x50000000

BLOCK_LAYOUT_ESCAPE             = 0x0   # lo nibble 0 halt, remainder is non-pointer (lo != 0 undef)
BLOCK_LAYOUT_NON_OBJECT_BYTES   = 0x1   # lo nibble # bytes non-objects
BLOCK_LAYOUT_NON_OBJECT_WORDS   = 0x2   # lo nibble # ptr-sized words non-objects
BLOCK_LAYOUT_STRONG             = 0x3   # lo nibble # strong pointers
BLOCK_LAYOUT_BYREF              = 0x4   # lo nibble # byref pointers
BLOCK_LAYOUT_WEAK               = 0x5   # lo nibble # weak pointers
BLOCK_LAYOUT_UNRETAINED         = 0x6   # lo nibble # unretained pointers


def _get_custom_type(bv, name, source):
    type_ = bv.get_type_by_name(name)
    if type_ is not None:
        return type_
    types = bv.parse_types_from_string(source)
    bv.define_types([(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, k), k, v) for k, v in types.types.items()], None)
    type_ = bv.get_type_by_name(name)
    assert type_ is not None
    return type_


def _get_objc_type(bv, name):
    """
    These are only loaded by Binary Ninja if it detects Objective C.
    However, libclosure can be used without Objective C and we still
    need these types.
    """
    return _get_custom_type(bv, name, _OBJC_TYPE_SOURCE)


def _get_libclosure_type(bv, name):
    """
    Get a type shipped with the plugin.
    On first use, will define all types
    come with the plugin.
    """
    # Make sure the ObjC types we use in _LIBCLOSURE_TYPE_SOURCE
    # are present before parsing.
    _ = _get_objc_type(bv, "Class")
    return _get_custom_type(bv, name, _LIBCLOSURE_TYPE_SOURCE)


def _define_ns_concrete_block_imports(bv):
    """
    For some reason, Binary Ninja does not reliably define all external symbols.
    Make sure __NSConcreteGlobalBlock and __NSConcreteStackBlock are defined
    appropriately.
    """
    class_type = _get_objc_type(bv, "Class")
    for sym_name in ("__NSConcreteGlobalBlock", "__NSConcreteStackBlock"):
        ext_sym = shinobi.get_symbol_of_type(bv, sym_name, binja.SymbolType.ExternalSymbol)
        if ext_sym is None:
            return
        shinobi.make_data_var(bv,
                              ext_sym.address,
                              class_type)


def append_layout_fields(bv, struct, layout, block_has_extended_layout, byref_indexes=None, layout_end_obj=None):
    """
    Append fields specified by layout to struct.
    If byref_indexes is given, the struct member index of all byref pointers is
    appended to byref_indexes.  If layout_end_obj is given, and layout is an
    extended layout bytecode, set layout_end_obj.layout_end to the end address
    of the bytecode.
    """
    if layout == 0:
        return
    if not block_has_extended_layout:
        # XXX
        return
    if layout < 0x1000:
        # compact encoding
        n_strong_ptrs = (layout >> 8) & 0xf
        n_byref_ptrs = (layout >> 4) & 0xf
        n_weak_ptrs = layout & 0xf
        for _ in range(n_strong_ptrs):
            struct.append(_get_objc_type(bv, "id"), f"strong_ptr_{struct.width:x}")
        for _ in range(n_byref_ptrs):
            if byref_indexes is not None:
                byref_indexes.append(len(struct.members))
            struct.append(_get_objc_type(bv, "id"), f"byref_ptr_{struct.width:x}")
        for _ in range(n_weak_ptrs):
            struct.append(_get_objc_type(bv, "id"), f"weak_ptr_{struct.width:x}")
    else:
        # bytecode encoding
        br = binja.BinaryReader(bv)
        br.seek(layout)
        while True:
            op = br.read8()
            opcode = (op & 0xf0) >> 4
            oparg = (op & 0x0f)
            if opcode == BLOCK_LAYOUT_ESCAPE:
                break
            elif opcode == BLOCK_LAYOUT_NON_OBJECT_BYTES:
                struct.append(bv.parse_type_string(f"uint8_t [{oparg}]")[0], f"non_object_{struct.width:x}")
            elif opcode == BLOCK_LAYOUT_NON_OBJECT_WORDS:
                for _ in range(oparg):
                    struct.append(bv.parse_type_string(f"uint64_t")[0], f"non_object_{struct.width:x}")
            elif opcode == BLOCK_LAYOUT_STRONG:
                for _ in range(oparg):
                    struct.append(_get_objc_type(bv, "id"), f"strong_ptr_{struct.width:x}")
            elif opcode == BLOCK_LAYOUT_BYREF:
                for _ in range(oparg):
                    if byref_indexes is not None:
                        byref_indexes.append(len(struct.members))
                    struct.append(_get_objc_type(bv, "id"), f"byref_ptr_{struct.width:x}")
            elif opcode == BLOCK_LAYOUT_WEAK:
                for _ in range(oparg):
                    struct.append(_get_objc_type(bv, "id"), f"weak_ptr_{struct.width:x}")
            elif opcode == BLOCK_LAYOUT_UNRETAINED:
                for _ in range(oparg):
                    struct.append(_get_objc_type(bv, "id"), f"unretained_ptr_{struct.width:x}")
            else:
                print(f"Warning: Unknown extended layout op {op:#04x}")
                break
        if layout_end_obj is not None:
            layout_end_obj.layout_end = br.offset


class BlockLiteral:
    @classmethod
    def from_data(cls, bv, bl_data_var):
        """
        Read block literal from data.
        """
        is_stack_block = False
        br = binja.BinaryReader(bv)
        br.seek(bl_data_var.address)
        isa = br.read64()
        flags = br.read32()
        reserved = br.read32()
        invoke = br.read64()
        descriptor = br.read64()
        return cls(bv, is_stack_block, bl_data_var, isa, flags, reserved, invoke, descriptor)

    @classmethod
    def from_stack(cls, bv, bl_insn, bl_var):
        is_stack_block = True
        bl_var.type = _get_libclosure_type(bv, "Block_literal")
        bl_insn = shinobi.reload_hlil_instruction(bv, bl_insn)

        if isinstance(bl_insn, binja.HighLevelILVarDeclare):
            stack_var_id = bl_insn.var.identifier
        elif isinstance(bl_insn, binja.HighLevelILVarInit):
            stack_var_id = bl_insn.dest.identifier
        elif isinstance(bl_insn, binja.HighLevelILAssign):
            if isinstance(bl_insn.dest, binja.HighLevelILStructField):
                stack_var_id = bl_insn.dest.src.var.identifier
            else:
                raise RuntimeError(f"bl_insn.dest after reload is unexpected type {type(bl_insn.dest).__name__}")
        else:
            raise RuntimeError(f"bl_insn after reload is unexpected type {type(bl_insn).__name__}")

        for insn in shinobi.yield_struct_field_assign_hlil_instructions_for_var_id(bl_insn.function, stack_var_id):
            if insn.dest.member_index == 0:
                if isinstance(insn.src, binja.HighLevelILImport) and str(insn.src) == '__NSConcreteStackBlock':
                    isa = insn.src.address
            elif insn.dest.member_index == 1:
                if isinstance(insn.src, binja.HighLevelILStructField):
                    raise RuntimeError("RHS of flags is struct field instead of constant.  If d8-d15/v8-v15 then likely because of Binja bug treating them as caller-saved when they are supposed to be callee-saved.")
                if isinstance(insn.src, (binja.HighLevelILConst,
                                         binja.HighLevelILConstPtr)):
                    flags = insn.src.constant
            elif insn.dest.member_index == 2:
                if isinstance(insn.src, (binja.HighLevelILConst,
                                         binja.HighLevelILConstPtr)):
                    reserved = insn.src.constant
                else:
                    reserved = None
            elif insn.dest.member_index == 3:
                if isinstance(insn.src, (binja.HighLevelILConst,
                                         binja.HighLevelILConstPtr)):
                    invoke = insn.src.constant
            elif insn.dest.member_index == 4:
                if isinstance(insn.src, (binja.HighLevelILConst,
                                         binja.HighLevelILConstPtr)):
                    descriptor = insn.src.constant
            else:
                # We don't know if the members are assigned in-order,
                # so we cannot rely on having descriptor and hence
                # size available.  As a result, do not attempt to pick
                # up imported variables here.  We'll need another pass
                # for that later.
                pass
            local_vars = locals()
            if all([vn in local_vars for vn in ('isa', 'flags', 'reserved', 'invoke', 'descriptor')]):
                break
        return cls(bv, is_stack_block, bl_insn, isa, flags, reserved, invoke, descriptor)

    def __init__(self, bv, is_stack_block, insn_or_data_var, isa, flags, reserved, invoke, descriptor):
        self._bv = bv
        self.is_stack_block = is_stack_block
        if self.is_stack_block:
            self.insn = insn_or_data_var
            self.data_var = None
            self.address = self.insn.address
        else:
            self.insn = None
            self.data_var = insn_or_data_var
            self.address = self.data_var.address
        self.isa = isa
        self.flags = flags
        self.reserved = reserved
        self.invoke = invoke
        self.descriptor = descriptor
        assert self.invoke != 0
        assert self.descriptor != 0
        if self.is_stack_block:
            assert (self.flags & BLOCK_IS_GLOBAL) == 0
        else:
            assert (self.flags & BLOCK_IS_GLOBAL) != 0

    def __str__(self):
        if self.is_stack_block:
            block = f"Stack block"
        else:
            block = f"Global block"
        return f"{block} at {self.address:x} with flags {self.flags:08x} invoke {self.invoke:x} descriptor {self.descriptor:x}"

    def annotate_literal(self, bd):
        """
        Annotate the block literal.
        """
        # Packed because block layout bytecode can lead to misaligned words,
        # which according to comments in LLVM source code seems intentional.
        struct = binja.StructureBuilder.create(packed=True)
        struct.append(_get_objc_type(self._bv, "Class"), "isa")
        struct.append(self._bv.parse_type_string(f"volatile uint32_t flags")[0], "flags")
        struct.append(self._bv.parse_type_string(f"uint32_t reserved")[0], "reserved")
        struct.append(_get_libclosure_type(self._bv, "BlockInvokeFunction"), "invoke")
        struct.append(binja.Type.pointer(self._bv.arch, _get_libclosure_type(self._bv, "Block_descriptor_1")), "descriptor") # placeholder
        if bd.imported_variables_size > 0:
            self.byref_indexes = []
            append_layout_fields(self._bv, struct, bd.layout, bd.block_has_extended_layout, self.byref_indexes, layout_end_obj=bd)
        self.struct_builder = struct
        self.struct_name = f"Block_literal_{self.address:x}"
        self._bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, self.struct_name), self.struct_name, self.struct_builder)
        self.struct_type_name = f"struct {self.struct_name}"
        self.struct_type = self._bv.parse_type_string(self.struct_type_name)[0]
        assert self.struct_type is not None
        if self.is_stack_block:
            if isinstance(self.insn, binja.HighLevelILVarDeclare):
                stack_var = self.insn.var
            elif isinstance(self.insn, binja.HighLevelILAssign):
                if isinstance(self.insn.dest, binja.HighLevelILStructField):
                    stack_var = self.insn.dest.src.var
                else:
                    raise RuntimeError(f"self.insn.dest is unexpected type {type(self.insn.dest).__name__}")
            else:
                raise RuntimeError(f"self.insn is unexpected type {type(self.insn).__name__}")
            stack_var_type_name = str(stack_var.type)
            if stack_var_type_name.startswith("struct Block_literal_") and stack_var_type_name != self.struct_type_name:
                # Stack var has already been annotated for initialization code
                # at a different address, likely because multiple branches in
                # the function place a block at the same stack address.
                # Unfortunately, this seems to be a hypothetical situation
                # right now, as Binja does not seem to handle different use of
                # the same stack area by different branches gracefully.
                self._bv.set_comment_at(self.address, f"Apple Blocks Plugin:\nStack var {stack_var.name} already annotated with type {stack_var_type_name}.\nDefined {self.struct_type_name} but did not clobber var type.\nSplitting the stack var might help here.")
                return

            if not stack_var.name.startswith("stack_block_"):
                stack_var.name = f"stack_block_{stack_var.name}"
            stack_var.type = self.struct_type_name
            self.insn = shinobi.reload_hlil_instruction(self._bv, self.insn)
        else:
            self.data_var.name = f"global_block_{self.address:x}"
            self.data_var.type = self.struct_type_name

        if self.struct_builder.width < bd.size:
            # The comment uses ###h instead of 0x### syntax to avoid Binja
            # interpreting hexadecimal sizes as clickable addresses.
            n_unaccounted = bd.size - struct.width
            self._bv.set_comment_at(self.address, f"Apple Blocks Plugin:\nBlock literal nominal size {bd.size:x}h.\n{self.struct_type_name} has width {self.struct_builder.width:x}h.\n{n_unaccounted:x}h bytes missing, add to struct manually.")
            # XXX try to pick up imported vars automatically

    def _type_for_ctype(self, ctype):
        if ctype.endswith("!"):
            fallback = 'id'
            ctype = ctype.replace("!", "*")
        elif ctype.endswith("*"):
            fallback = 'void *'
        else:
            fallback = 'void'
        try:
            return self._bv.parse_type_string(ctype)[0]
        except SyntaxError:
            # XXX if struct or union and we have member type info, create struct or union and retry
            return self._bv.parse_type_string(fallback)[0]

    def annotate_layout_bytecode(self, bd):
        if bd.block_has_signature and bd.block_has_extended_layout and bd.layout >= 0x1000:
            n = bd.layout_end - bd.layout
            shinobi.make_data_var(self._bv,
                                  bd.layout,
                                  self._bv.parse_type_string(f"uint8_t [{n}]")[0],
                                  f"block_layout_{bd.layout:x}")

    def annotate_functions(self, bd):
        """
        Annotate the invoke function as well as the copy and dispose functions, if they exist.
        """
        invoke_func = self._bv.get_function_at(self.invoke)
        if invoke_func is not None:
            if bd.signature_raw is not None:
                # This works well for most blocks, but because Binja does
                # not seem to support [Apple's variant of] AArch64 calling
                # conventions properly when things are passed in v registers
                # or on the stack, signatures are sometimes wrong.  I find
                # it useful to have them, even if they are sometimes wrong.
                # The types assigned here should be correct, assuming no
                # fallbacks to void were required (those may cause size to
                # be lost, which for structs by value determines if they
                # get passed in multiple registers or on the stack).
                try:
                    ctypes = objctypes.ObjCEncodedTypes(bd.signature_raw).ctypes
                    assert len(ctypes) > 0
                    types = list(map(self._type_for_ctype, ctypes))
                    types[1] = binja.Type.pointer(self._bv.arch, self.struct_type)
                    func_type = binja.Type.function(types[0], types[1:])
                except NotImplementedError as e:
                    print(f"Failed to parse ObjC type encoding {bd.signature_raw!r}: {type(e).__name__}: {e}", file=sys.stderr)
                    func_type = None
            else:
                # No signature string.
                func_type = None

            if func_type is None and len(invoke_func.parameter_vars) == 0:
                # If Binja did not pick up on any parameters, fall back to a vararg
                # signature.  We're not going to clobber any parameter types.
                func_type = binja.Type.function(binja.Type.void(), [binja.Type.pointer(self._bv.arch, self.struct_type)], variable_arguments=True)

            if func_type is None:
                # Finally fall back to surgically setting return and first argument
                # types, leaving the other parameters undisturbed.
                invoke_func.return_type = binja.Type.void()
                invoke_func.parameter_vars[0].set_name_and_type_async("block", binja.Type.pointer(self._bv.arch, self.struct_type))
                self._bv.update_analysis_and_wait()

            else:
                # Set function type.

                # As of Binja 4.2, the setter for Function.type does not
                # update_analysis_and_wait(), unlike the Variable.name and
                # Variable.type setters that do.  Also, the setter for
                # Variable.name is not atomic; it will first copy the current
                # type, then proceed to set both name and type on the variable.
                # As a result, we need to update_analysis_and_wait() manually
                # to avoid an easy-to-repro race condition where a subsequent
                # assignment to Variable.name while the Function.type
                # assignment is still in flight may clobber the type for the
                # first parameter with the type it had before assigning the
                # function type.
                invoke_func.type = func_type
                self._bv.update_analysis_and_wait()

                if len(invoke_func.parameter_vars) >= 1:
                    invoke_func.parameter_vars[0].name = "block"

                # propagate invoke function signature to invoke pointer on block literal
                invoke_pointer_index = self.struct_builder.index_by_name("invoke")
                self.struct_builder.replace(invoke_pointer_index,
                                            binja.Type.pointer(self._bv.arch, func_type), "invoke")
                self._bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, self.struct_name),
                                     self.struct_name, self.struct_builder)
                self.struct_type = self._bv.parse_type_string(self.struct_type_name)[0]

            if invoke_func.name == f"sub_{invoke_func.start:x}":
                invoke_func.name = f"sub_{invoke_func.start:x}_block_invoke"

        if bd.block_has_copy_dispose:
            # Interleave annotation of the two functions in order to minimize
            # the number of expensive calls to update_analysis_and_wait().
            copy_func = self._bv.get_function_at(bd.copy)
            dispose_func = self._bv.get_function_at(bd.dispose)
            if copy_func is not None or dispose_func is not None:
                if copy_func is not None:
                    copy_func.type = binja.Type.function(binja.Type.void(), [binja.Type.pointer(self._bv.arch, self.struct_type),
                                                                             binja.Type.pointer(self._bv.arch, self.struct_type)])
                if dispose_func is not None:
                    dispose_func.type = binja.Type.function(binja.Type.void(), [binja.Type.pointer(self._bv.arch, self.struct_type)])
                self._bv.update_analysis_and_wait()

                if copy_func is not None:
                    if len(copy_func.parameter_vars) >= 2:
                        copy_func.parameter_vars[0].set_name_async("dst")
                        copy_func.parameter_vars[1].set_name_async("src")
                if dispose_func is not None:
                    if len(dispose_func.parameter_vars) >= 1:
                        dispose_func.parameter_vars[0].set_name_async("dst")
                self._bv.update_analysis_and_wait()

                if copy_func is not None:
                    if copy_func.name == f"sub_{copy_func.start:x}":
                        copy_func.name = f"sub_{copy_func.start:x}_block_copy"

                if dispose_func is not None:
                    if dispose_func.name == f"sub_{dispose_func.start:x}":
                        dispose_func.name = f"sub_{dispose_func.start:x}_block_dispose"


class BlockDescriptor:
    def __init__(self, bv, descriptor_address, block_flags):
        """
        Read block descriptor from data.
        """
        self._bv = bv
        self.address = descriptor_address
        self.block_flags = block_flags

        br = binja.BinaryReader(self._bv)
        br.seek(self.address)
        self.reserved = br.read64()
        self.size = br.read64()
        assert self.size >= 0x20
        if self.block_has_copy_dispose:
            self.copy = br.read64()
            self.dispose = br.read64()
        else:
            self.copy = None
            self.dispose = None
        if self.block_has_signature:
            self.signature = br.read64()
            if self.signature != 0:
                self.signature_raw = self._bv.get_ascii_string_at(self.signature, 0).raw
            else:
                self.signature_raw = None
            self.layout = br.read64()

    @property
    def imported_variables_size(self):
        return self.size - 0x20

    # XXX clean these up, probably want to move them to bl, pass bl to ctor
    @property
    def block_has_copy_dispose(self):
        return (self.block_flags & BLOCK_HAS_COPY_DISPOSE) != 0

    @property
    def block_has_signature(self):
        return (self.block_flags & BLOCK_HAS_SIGNATURE) != 0

    @property
    def block_has_extended_layout(self):
        return (self.block_flags & BLOCK_HAS_EXTENDED_LAYOUT) != 0

    @property
    def block_is_global(self):
        return (self.block_flags & BLOCK_IS_GLOBAL) != 0

    def __str__(self):
        return f"Block descriptor at {self.address:x} size {self.size:#x}"

    def annotate_descriptor(self, bl):
        """
        Annotate block descriptor.
        """
        struct = binja.StructureBuilder.create()
        struct.append(self._bv.parse_type_string("uint64_t reserved")[0], "reserved")
        struct.append(self._bv.parse_type_string("uint64_t size")[0], "size")
        if self.block_has_copy_dispose:
            struct.append(_get_libclosure_type(self._bv, "BlockCopyFunction"), "copy")
            struct.append(_get_libclosure_type(self._bv, "BlockDisposeFunction"), "dispose")
        if self.block_has_signature:
            struct.append(self._bv.parse_type_string("char const *signature")[0], "signature")
            if self.layout != 0 and self.block_has_extended_layout:
                if self.layout < 0x1000:
                    struct.append(self._bv.parse_type_string("uint64_t layout")[0], "layout")
                else:
                    struct.append(self._bv.parse_type_string("uint8_t const *layout")[0], "layout")
            else:
                # XXX non-extended layout or layout 0
                struct.append(self._bv.parse_type_string("void *layout")[0], "layout")
        self.struct_builder = struct
        self.struct_name = f"Block_descriptor_{self.address:x}"
        self._bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, self.struct_name), self.struct_name, self.struct_builder)
        self.struct_type_name = f"struct {self.struct_name}"
        self.struct_type = self._bv.parse_type_string(self.struct_type_name)[0]
        assert self.struct_type is not None
        shinobi.make_data_var(self._bv,
                              self.address,
                              self.struct_type,
                              f"block_descriptor_{self.address:x}")

        # propagate struct type to descriptor pointer on block literal
        pointer_index = bl.struct_builder.index_by_name("descriptor")
        bl.struct_builder.replace(pointer_index, binja.Type.pointer(self._bv.arch, self.struct_type), "descriptor")
        self._bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, bl.struct_name), bl.struct_name, bl.struct_builder)
        bl.struct_type = self._bv.parse_type_string(bl.struct_type_name)[0]


def annotate_global_block_literal(bv, block_literal_addr):
    where = f"global block {block_literal_addr:x}"

    print(f"Annotating {where}")

    block_literal_data_var = bv.get_data_var_at(block_literal_addr)
    if block_literal_data_var is None:
        # We only expect this to happen for manual invocation, not
        # for the automatic sweep, as the sweep requires data
        # references in order to pick up a global block instance.
        class_type = _get_objc_type(bv, "Class")
        bv.define_user_data_var(block_literal_addr, binja.Type.pointer(bv.arch, class_type))
        block_literal_data_var = bv.get_data_var_at(block_literal_addr)
        assert block_literal_data_var is not None

    data_var_value = block_literal_data_var.value
    if isinstance(data_var_value, dict) and 'isa' in data_var_value:
        data_var_value = data_var_value['isa']
    if not isinstance(data_var_value, int):
        print(f"{where}: Data var has value {data_var_value} of type {type(data_var_value).__name__}, expected int, fix plugin", file=sys.stderr)
        return
    ext_sym = shinobi.get_symbol_of_type(bv, "__NSConcreteGlobalBlock", binja.SymbolType.ExternalSymbol)
    if ext_sym is None:
        print(f"__NSConcreteGlobalBlock not found", file=sys.stderr)
        return
    if not data_var_value == ext_sym.address:
        print(f"{where}: Data var has value {data_var_value:x} instead of {ext_sym.address:x} __NSConcreteGlobalBlock", file=sys.stderr)
        return

    try:
        bl = BlockLiteral.from_data(bv, block_literal_data_var)
        print(bl)
        bd = BlockDescriptor(bv, bl.descriptor, bl.flags)
        print(bd)
        bl.annotate_literal(bd)
        bd.annotate_descriptor(bl)
        bl.annotate_layout_bytecode(bd)
        bl.annotate_functions(bd)
    except Exception as e:
        print(f"{where}: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return


def annotate_stack_block_literal(bv, block_literal_insn):
    where = f"stack block {block_literal_insn.address:x}"

    print(f"Annotating {where}")

    if isinstance(block_literal_insn, binja.HighLevelILVarInit):
        # The most common case where Binja knows nothing about the stack
        # variable.  The initialization with __NSConcreteStackBlock is a
        # HighLevelILVarInit.
        block_literal_var = block_literal_insn.dest
        isa_src = block_literal_insn.src
    elif isinstance(block_literal_insn, binja.HighLevelILAssign):
        # Sometimes use of the block in subsequent APIs with known signature
        # (e.g. __Block_copy) causes Binja to create a stack var at the stack
        # address of the block literal.  The initialization with
        # __NSConcreteStackBlock is a HighLevelILAssign.
        if isinstance(block_literal_insn.dest, binja.HighLevelILStructField):
            block_literal_var = block_literal_insn.dest.src.var
        elif isinstance(block_literal_insn.dest, binja.HighLevelILVar):
            block_literal_var = block_literal_insn.dest.var
        else:
            print(f"{where}: Assignment is not to a var or to a struct field", file=sys.stderr)
            return
        isa_src = block_literal_insn.src
    else:
        print(f"{where}: Instruction is neither a var init nor an assign", file=sys.stderr)
        return

    if block_literal_var.source_type != binja.VariableSourceType.StackVariableSourceType:
        print(f"{where}: Assignment is not to a stack variable (var source_type is {block_literal_var.source_type!r})", file=sys.stderr)
        return

    if not isinstance(isa_src, binja.HighLevelILImport) or str(isa_src) != '__NSConcreteStackBlock':
        print(f"{where}: RHS is not HighLevelILImport of __NSConcreteStackBlock", file=sys.stderr)
        return

    try:
        bl = BlockLiteral.from_stack(bv, block_literal_insn, block_literal_var)
        print(bl)
        bd = BlockDescriptor(bv, bl.descriptor, bl.flags)
        print(bd)
        bl.annotate_literal(bd)
        bd.annotate_descriptor(bl)
        bl.annotate_layout_bytecode(bd)
        bl.annotate_functions(bd)
    except Exception as e:
        print(f"{where}: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return

    # XXX refactor byref handling

    # annotate stack byrefs

    try:
        if bd.imported_variables_size > 0 and len(bl.byref_indexes) > 0:
            byref_srcs = []
            byref_indexes_set = set(bl.byref_indexes)
            for insn in shinobi.yield_struct_field_assign_hlil_instructions_for_var_id(bl.insn.function, bl.insn.var.identifier):
                if isinstance(insn.src, binja.HighLevelILAddressOf):
                    insn_src = insn.src
                elif isinstance(insn.src, (binja.HighLevelILDerefField,
                                           binja.HighLevelILDeref,
                                           binja.HighLevelILImport,
                                           binja.HighLevelILConst,
                                           binja.HighLevelILConstPtr,
                                           binja.HighLevelILCall,
                                           binja.HighLevelILStructField,
                                           binja.HighLevelILVar)):
                    # binja.HighLevelILStructField is typically because of Binja bug
                    # treating d8-15/v8-15 as caller-saved instead of callee-saved.
                    # binja.HighLevelILVar is typically a byref passed as argument.
                    insn_src = None
                else:
                    print(f"{where}: Skipping assignment right-hand-side for {insn.src!r}, fix plugin", file=sys.stderr)
                    continue
                if insn.dest.member_index in byref_indexes_set:
                    byref_srcs.append((insn_src, insn.dest.member_index))

            assert len(byref_srcs) == len(bl.byref_indexes)
            for byref_src, byref_member_index in byref_srcs:
                if byref_src is None:
                    continue
                assert isinstance(byref_src, binja.HighLevelILAddressOf)
                if isinstance(byref_src.src, binja.HighLevelILVar):
                    var_id = byref_src.src.var.identifier
                else:
                    print(f"{where}: Byref src var {byref_src} src is {type(byref_src.src).__name__}: Annotate manually", file=sys.stderr)
                    continue

                byref_insn = None
                for insn in bl.insn.function.instructions:
                    if isinstance(insn, binja.HighLevelILVarDeclare):
                        cand_var = insn.var
                    elif isinstance(insn, binja.HighLevelILVarInit):
                        if isinstance(insn.dest, binja.HighLevelILStructField):
                            continue
                        cand_var = insn.dest
                    else:
                        continue

                    if cand_var.identifier == var_id:
                        byref_insn = insn
                        byref_insn_var = cand_var
                        break

                else:
                    print(f"{where}: Byref src var {byref_src} id {var_id:x} not found in function's var declarations and inits", file=sys.stderr)
                    continue

                # So apparently this works; despite the reloads, byref_srcs are not invalidated, identifiers are still current.
                # Should that cease to be the case, we'll need to find next byref_src in a way that is robust to reloads.

                byref_insn_var.name = f"block_byref_{byref_insn_var.name}"

                byref_struct = binja.StructureBuilder.create()
                byref_struct.append(_get_objc_type(bv, "Class"), "isa")
                byref_struct.append(bv.parse_type_string("void *forwarding")[0], "forwarding") # placeholder
                byref_struct.append(bv.parse_type_string("volatile int32_t flags")[0], "flags")
                byref_struct.append(bv.parse_type_string("uint32_t size")[0], "size")

                byref_insn_var.type = byref_struct
                byref_insn = shinobi.reload_hlil_instruction(bv, byref_insn)
                byref_insn_var = byref_insn.var

                for insn in shinobi.yield_struct_field_assign_hlil_instructions_for_var_id(byref_insn.function, byref_insn_var.identifier):
                    # 0 isa
                    # 1 forwarding
                    if insn.dest.member_index == 2:
                        if isinstance(insn.src, (binja.HighLevelILConst,
                                                 binja.HighLevelILConstPtr)):
                            byref_flags = insn.src.constant
                    elif insn.dest.member_index == 3:
                        if isinstance(insn.src, (binja.HighLevelILConst,
                                                 binja.HighLevelILConstPtr)):
                            byref_size = insn.src.constant
                try:
                    print(f"Block byref at {byref_insn.address:x} flags {byref_flags:08x} size {byref_size:#x}")
                except UnboundLocalError as e:
                    print(f"Block byref at {byref_insn.address:x} failed to find flags or size assignments", file=sys.stderr)
                    continue

                if (byref_flags & BLOCK_BYREF_HAS_COPY_DISPOSE) != 0:
                    byref_struct.append(_get_libclosure_type(bv, "BlockByrefKeepFunction"), "byref_keep")
                    byref_struct.append(_get_libclosure_type(bv, "BlockByrefDestroyFunction"), "byref_destroy")
                byref_layout_nibble = (byref_flags & BLOCK_BYREF_LAYOUT_MASK)
                if byref_layout_nibble == BLOCK_BYREF_LAYOUT_EXTENDED:
                    byref_struct.append(bv.parse_type_string("void *layout")[0], "layout")
                    layout_index = byref_struct.index_by_name("layout")
                    byref_insn_var.type = byref_struct
                    byref_insn = shinobi.reload_hlil_instruction(bv, byref_insn)
                    byref_insn_var = byref_insn.var
                    for insn in shinobi.yield_struct_field_assign_hlil_instructions_for_var_id(byref_insn.function, byref_insn_var.identifier):
                        if insn.dest.member_index == layout_index:
                            assert isinstance(insn.src, (binja.HighLevelILConst,
                                                         binja.HighLevelILConstPtr))
                            byref_layout = insn.src.constant
                            break
                    if byref_layout != 0:
                        if byref_layout < 0x1000:
                            byref_struct.replace(layout_index, bv.parse_type_string("uint64_t layout")[0], "layout")
                        else:
                            byref_struct.replace(layout_index, bv.parse_type_string("uint8_t const *layout")[0], "layout")
                    append_layout_fields(bv, byref_struct, byref_layout, block_has_extended_layout=True)
                elif byref_layout_nibble == BLOCK_BYREF_LAYOUT_NON_OBJECT:
                    byref_struct.append(bv.parse_type_string("uint64_t non_object_0")[0], "non_object_0")
                elif byref_layout_nibble == BLOCK_BYREF_LAYOUT_STRONG:
                    byref_struct.append(_get_objc_type(bv, "id"), "strong_ptr_0")
                elif byref_layout_nibble == BLOCK_BYREF_LAYOUT_WEAK:
                    byref_struct.append(_get_objc_type(bv, "id"), "weak_ptr_0")
                elif byref_layout_nibble == BLOCK_BYREF_LAYOUT_UNRETAINED:
                    byref_struct.append(_get_objc_type(bv, "id"), "unretained_ptr_0")

                byref_struct_name = f"Block_byref_{byref_insn.address:x}"
                bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, byref_struct_name), byref_struct_name, byref_struct)
                byref_struct_type_name = f"struct {byref_struct_name}"
                byref_struct_type = bv.parse_type_string(byref_struct_type_name)[0]
                assert byref_struct_type is not None

                # propagate registered struct to forwarding self pointer
                byref_struct.replace(1, binja.Type.pointer(bv.arch, byref_struct_type), "forwarding")
                bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, byref_struct_name), byref_struct_name, byref_struct)
                byref_struct_type = bv.parse_type_string(byref_struct_type_name)[0]

                byref_insn_var.type = byref_struct_type

                # propagate byref type to block literal type
                byref_member_name = bl.struct_builder.members[byref_member_index].name
                assert byref_member_name.startswith("byref_ptr_")
                bl.struct_builder.replace(byref_member_index, binja.Type.pointer(bv.arch, byref_struct_type), byref_member_name)
                bv.define_type(binja.Type.generate_auto_type_id(_TYPE_ID_SOURCE, bl.struct_name), bl.struct_name, bl.struct_builder)
                bl.struct_type = bv.parse_type_string(bl.struct_type_name)[0]

                # XXX annotate functions, which is often hard with the use of
                # callee-saved D/V registers treated as caller-saved in HLIL;
                # it seems that Binja does not properly deal with the fact that
                # D8-15 are callee-saved but the rest of V8-15 are caller-saved.

    except Exception as e:
        print(f"{where}: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return


def annotate_all_global_blocks(bv, set_progress=None):
    ext_sym = shinobi.get_symbol_of_type(bv, "__NSConcreteGlobalBlock", binja.SymbolType.ExternalSymbol)
    if ext_sym is None:
        print("__NSConcreteGlobalBlock not found, target does not appear to contain any global blocks")
        return
    assert ext_sym.address is not None and ext_sym.address != 0
    for addr in bv.get_data_refs(ext_sym.address):
        if set_progress is not None:
            set_progress(f"{addr:x}")
        annotate_global_block_literal(bv, addr)


def annotate_all_stack_blocks(bv, set_progress=None):
    imp_data_sym = shinobi.get_symbol_of_type(bv, "__NSConcreteStackBlock", binja.SymbolType.ImportedDataSymbol)
    imp_addr_sym = shinobi.get_symbol_of_type(bv, "__NSConcreteStackBlock", binja.SymbolType.ImportAddressSymbol)
    imp_sym = imp_data_sym or imp_addr_sym or None
    if imp_sym is None:
        print("__NSConcreteStackBlock not found, target does not appear to contain any stack blocks")
        return
    # We'd want to use get_code_refs here, but it is very unreliable.
    # Yielded refsrc objects often have only llil but not mlil or hlil;
    # .llil.hlil is also None, .llil.hlils contains the llil that matches,
    # sometimes multiple times.  The issue seems more frequent on but not
    # limited to arm64.
    #for refsrc in bv.get_code_refs(imp_sym.address):
    #    print(refsrc)
    #    print(refsrc.llil, refsrc.mlil, refsrc.hlil, refsrc.llil.hlil, refsrc.llil.hlils)
    for insn in bv.hlil_instructions:
        if not isinstance(insn, binja.HighLevelILVarInit):
            continue
        if not isinstance(insn.src, binja.HighLevelILImport):
            continue
        if insn.src.constant != imp_sym.address:
            continue
        if set_progress is not None:
            set_progress(f"{insn.address:x}")
        annotate_stack_block_literal(bv, insn)


@shinobi.register_for_high_level_il_instruction("Blocks\\Annotate stack block here", is_valid=is_valid)
@shinobi.background_task("Blocks: Stack block")
@shinobi.undoable
def plugin_cmd_annotate_stack_block_literal_here(bv, block_literal_insn, set_progress=None):
    """
    Define a stack block literal here.
    """
    annotate_stack_block_literal(bv, block_literal_insn)


@shinobi.register_for_address("Blocks\\Annotate global block here", is_valid=is_valid)
@shinobi.background_task("Blocks: Global block")
@shinobi.undoable
def plugin_cmd_annotate_global_block_literal_here(bv, address, set_progress=None):
    """
    Define a global block literal here.
    """
    annotate_global_block_literal(bv, address)


@shinobi.register("Blocks\\Annotate all stack blocks", is_valid=is_valid)
@shinobi.background_task("Blocks: All stack blocks")
@shinobi.undoable
def plugin_cmd_annotate_all_stack_blocks(bv, set_progress=None):
    """
    Look for all occurences of __NSConcreteStackBlock and
    annotate stack blocks where references are found.
    """
    _define_ns_concrete_block_imports(bv)
    annotate_all_stack_blocks(bv, set_progress=set_progress)


@shinobi.register("Blocks\\Annotate all global blocks", is_valid=is_valid)
@shinobi.background_task("Blocks: All global blocks")
@shinobi.undoable
def plugin_cmd_annotate_all_global_blocks(bv, set_progress=None):
    """
    Look for all occurences of __NSConcreteGlobalBlock and
    annotate global blocks where references are found.
    """
    _define_ns_concrete_block_imports(bv)
    annotate_all_global_blocks(bv, set_progress=set_progress)


@shinobi.register("Blocks\\Annotate all blocks", is_valid=is_valid)
@shinobi.background_task("Blocks: All blocks")
@shinobi.undoable
def plugin_cmd_annotate_all_stack_blocks(bv, set_progress=None):
    """
    Look for all occurences of __NSConcreteGlobalBlock and __NSConcreteStackBlock
    and annotate all blocks where references are found.
    """
    _define_ns_concrete_block_imports(bv)
    annotate_all_global_blocks(bv, set_progress=set_progress)
    annotate_all_stack_blocks(bv, set_progress=set_progress)

@shinobi.register_for_address("Blocks\\Remove plugin comment here", is_valid=is_valid)
@shinobi.background_task("Blocks: Remove comment")
@shinobi.undoable
def plugin_cmd_remove_plugin_comment_here(bv, address, set_progress=None):
    """
    Remove global comment here.
    Useful to remove comments added by this plugin, e.g. after manually
    adding missing imported variables to block literals.
    """
    bv.set_comment_at(address, None)
