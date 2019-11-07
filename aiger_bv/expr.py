from functools import partial, reduce
from typing import Union

import attr
import funcy as fn

from aiger_bv import aigbv
from aiger_bv import common as cmn


def constk(k, size=None):
    def _constk(expr):
        nonlocal size
        if size is None:
            size = expr.size
        return cmn.source(size, k, signed=False) \
            | cmn.sink(expr.size, expr.inputs)
    return _constk


@attr.s(frozen=True, slots=True, eq=False, auto_attribs=True)
class UnsignedBVExpr:
    aigbv: aigbv.AIGBV

    def __call__(self, inputs=None):
        if inputs is None:
            inputs = {}
        return self.aigbv(inputs)[0][self.output]

    def __getitem__(self, idx: Union[int, slice]):
        # TODO: support slice steps.
        def _indexer(idx):
            return cmn.index_gate(self.size, idx, self.output, cmn._fresh())

        if not isinstance(idx, slice):
            idx = slice(idx, idx+1)
        if idx.start is None:
            idx = slice(0, idx.stop)
        if idx.stop is None:
            idx = slice(idx.start, self.size)
        if idx.step is not None:
            raise NotImplementedError

        assert 0 <= idx.start < idx.stop <= self.size
        indicies = list(range(idx.start, idx.stop))

        if len(indicies) == 1:
            return UnsignedBVExpr(self.aigbv >> _indexer(indicies[0]))

        indexers = map(_indexer, indicies)
        indexer = reduce(lambda x, y: x | y, indexers)
        return UnsignedBVExpr(self.aigbv >> indexer)

    def concat(self, other):
        combiner = cmn.combine_gate(
            output=cmn._fresh(),
            left_wordlen=self.size, left=self.output,
            right_wordlen=other.size, right=other.output,
        )
        circ = self.aigbv | other.aigbv
        return type(self)(circ >> combiner)

    def repeat(self, times):
        # TODO: support size != 1 via self concatenation.
        assert self.size == 1
        repeater = cmn.repeat(times, self.output, cmn._fresh())
        return type(self)(self.aigbv >> repeater)

    @property
    def output(self):
        return fn.first(self.aigbv.outputs)

    @property
    def inputs(self):
        return self.aigbv.inputs

    @property
    def size(self):
        return fn.first(self.aigbv.omap.values())

    @property
    def aig(self):
        return self.aigbv.aig

    def __invert__(self):
        return _unary_gate(cmn.bitwise_negate, self)

    def __lshift__(self, n_bits):
        return _shift_gate(cmn.left_shift_gate, self, n_bits)

    def __rshift__(self, n_bits):
        return _shift_gate(cmn.logical_right_shift_gate, self, n_bits)

    def __add__(self, other):
        return _binary_gate(cmn.add_gate, self, other, lambda x: x)

    def __sub__(self, other):
        return _binary_gate(cmn.subtract_gate, self, other, constk(0))

    def __and__(self, other):
        return _binary_gate(cmn.bitwise_and, self, other, lambda x: x)

    def __matmul__(self, other):
        return _binary_gate(
            cmn.dot_mod2_gate, self, other,
            lambda e: _unary_gate(cmn.even_popcount_gate, e).aigbv
        )

    def __or__(self, other):
        return _binary_gate(cmn.bitwise_or, self, other, lambda x: x)

    def __xor__(self, other):
        return _binary_gate(cmn.bitwise_xor, self, other, constk(0))

    def __ne__(self, other):
        return _binary_gate(cmn.ne_gate, self, other, constk(0, 1))

    def __eq__(self, other):
        return ~(self != other)

    def __le__(self, other):
        return _binary_gate(cmn.unsigned_le_gate, self, other, constk(1, 1))

    def __ge__(self, other):
        return _binary_gate(cmn.unsigned_ge_gate, self, other, constk(1, 1))

    def __lt__(self, other):
        return ~(self >= other)

    def __gt__(self, other):
        return ~(self <= other)

    def __abs__(self):
        return self

    def with_output(self, val):
        return attr.evolve(self, aigbv=self.aigbv['o', {self.output: val}])

    def _fresh_output(self):
        return self.with_output(cmn._fresh())


class SignedBVExpr(UnsignedBVExpr):
    def __neg__(self):
        return _unary_gate(cmn.negate_gate, self)

    def __le__(self, other):
        return _binary_gate(cmn.signed_le_gate, self, other, constk(1, 1))

    def __ge__(self, other):
        return _binary_gate(cmn.signed_ge_gate, self, other, constk(1, 1))

    def __rshift__(self, n_bits):
        return _shift_gate(cmn.arithmetic_right_shift_gate, self, n_bits)

    def __abs__(self):
        return _unary_gate(cmn.abs_gate, self)


Expr = Union[UnsignedBVExpr, SignedBVExpr]


def _shift_gate(gate, expr, n_bits):
    return _unary_gate(gate=partial(gate, shift=n_bits), expr=expr)


def _binary_gate(gate, expr1, expr2, same_circ=None):
    if isinstance(expr2, int):
        expr2 = atom(expr1.size, expr2, signed=isinstance(expr1, SignedBVExpr))

    assert expr1.size == expr2.size
    if expr1.aigbv == expr2.aigbv and same_circ is not None:
        return type(expr1)(same_circ(expr1))
    elif expr1.aigbv.aig.outputs & expr2.aigbv.aig.outputs:
        expr2 = expr2._fresh_output()

    circ3 = expr1.aigbv | expr2.aigbv
    circ3 >>= gate(wordlen=expr1.size, output=cmn._fresh(),
                   left=expr1.output, right=expr2.output)
    return type(expr1)(aigbv=circ3)


def _unary_gate(gate, expr):
    circ = gate(expr.size, input=expr.output, output=cmn._fresh())
    return type(expr)(aigbv=expr.aigbv >> circ)


def ite(test, expr_true, expr_false):
    assert test.size == 1
    assert expr_true.size == expr_false.size
    test = test.repeat(expr_true.size)
    return (~test | expr_true) & (test | expr_false)


Val = Union[str, int, None]


def atom(wordlen: int, val: Val, signed: bool = True) -> Expr:
    output = cmn._fresh()
    if val is None:
        val = cmn._fresh()

    if isinstance(val, str):
        aig = cmn.identity_gate(wordlen, val, output)
    else:
        aig = cmn.source(wordlen, val, output, signed)

    return (SignedBVExpr if signed else UnsignedBVExpr)(aig)
