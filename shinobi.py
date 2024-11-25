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


def register(label, *args, **kvargs):
    """
    Decorator to register the decorated function as a general plugin command.
    """
    def decorator(func):
        binja.PluginCommand.register(label, func.__doc__, func, *args, **kvargs)
        return func
    return decorator


def register_for_function(label, *args, **kvargs):
    """
    Decorator to register the decorated function as a function plugin command.
    """
    def decorator(func):
        binja.PluginCommand.register_for_function(label, func.__doc__, func, *args, **kvargs)
        return func
    return decorator


def register_for_address(label, *args, **kvargs):
    """
    Decorator to register the decorated function as an address plugin command.
    """
    def decorator(func):
        binja.PluginCommand.register_for_address(label, func.__doc__, func, *args, **kvargs)
        return func
    return decorator


def register_for_high_level_il_instruction(label, *args, **kvargs):
    """
    Decorator to register the decorated function as a HLIL instruction plugin command.
    """
    def decorator(func):
        binja.PluginCommand.register_for_high_level_il_instruction(label, func.__doc__, func, *args, **kvargs)
        return func
    return decorator


class Task(binja.plugin.BackgroundTaskThread):
    """
    Helper class to run an analysis on a background thread.
    """

    def __init__(self, label, func, *args, **kvargs):
        super().__init__(label, False)
        self._label = label
        self._func = func
        self._args = args
        self._kvargs = kvargs

    def set_progress(self, text):
        self.progress = f"{self._label}...{text}"

    def run(self):
        self._func(*self._args, **(self._kvargs | {'set_progress': self.set_progress}))
        self.finish()

    @classmethod
    def spawn(cls, label, func, *args, **kvargs):
        task = cls(label, func, *args, **kvargs)
        task.start()


def background_task(label="Plugin action"):
    """
    Decorator for plugin command functions to run them on a
    background thread using Task.
    This is useful, because some of Binary Ninja's APIs refuse
    to work on the main thread or on a UI thread.
    Unfortunately, despite running on a background thread,
    there is still a lot of beach-balling going on in the UI.
    But at least all the APIs can be used.
    """
    def decorator(func):
        def closure(*args, **kvargs):
            Task.spawn(label, func, *args, **kvargs)
        closure.__doc__ = func.__doc__
        return closure
    return decorator


def yield_symbols_of_type(bv, name, type_):
    """
    Find all symbols of a specific type and return a generator for them.
    """
    for sym in filter(lambda x: x.type == type_, bv.symbols.get(name, [])):
        yield sym


def get_symbol_of_type(bv, name, type_):
    """
    Find a symbol of a specific type and return the first one found.
    """
    try:
        return next(yield_symbols_of_type(bv, name, type_))
    except StopIteration:
        return None


def reload_hlil_instruction(bv, hlil_insn):
    """
    Refresh the instruction and the function it is associated with.
    This is useful after setting the type of an operand in situations
    where there is a need to examine the instruction and function
    after applying the new type.
    This is based on the assumption that the HLIL instruction is the
    first or only HLIL instruction at its address.  If the number of
    HLIL instructions at the address changes across the reload, then
    this returns the first instruction at the address.
    """
    reloaded_func = bv.get_function_at(hlil_insn.function.source_function.start)
    for insn in reloaded_func.hlil.instructions:
        if insn.address == hlil_insn.address:
            reloaded_insn = insn
            break
    else:
        assert False
    assert reloaded_insn is not None
    return reloaded_insn


def yield_struct_field_assign_hlil_instructions_for_var_id(hlil_func, var_id):
    """
    Find all HLIL instructions that assign to struct fields of
    a struct with a given variable identifier.

    Note that variable identifiers may change across type changes
    in the function.
    """
    for insn in hlil_func.instructions:
        if not isinstance(insn, binja.HighLevelILAssign):
            continue
        if not isinstance(insn.dest, binja.HighLevelILStructField):
            continue

        if isinstance(insn.dest.src, binja.HighLevelILVar):
            stack_var = insn.dest.src.var
        elif isinstance(insn.dest.src, binja.HighLevelILArrayIndex):
            if not isinstance(insn.dest.src.src, binja.HighLevelILVar):
                continue
            stack_var = insn.dest.src.src.var
        elif isinstance(insn.dest.src, binja.HighLevelILStructField):
            if not isinstance(insn.dest.src.src, binja.HighLevelILVar):
                continue
            stack_var = insn.dest.src.src.var
        elif isinstance(insn.dest.src, binja.HighLevelILDerefField):
            continue
        else:
            raise NotImplementedError(f"Unhandled destination source type {type(insn.dest.src).__name__} in assign insn {insn!r}, fix plugin")

        if stack_var.identifier != var_id:
            continue

        yield insn
