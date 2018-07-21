import operator as op
from functools import reduce
from typing import Tuple, FrozenSet, NamedTuple, Union, Mapping, List

import funcy as fn

from aiger import aig, common, parser


BV_MAP = FrozenSet[Tuple[str, Tuple[str]]]


def _blast(bvname2vals, name_map):
    return fn.merge(
        *(dict(zip(names, bvname2vals[bvname])) for bvname, names in name_map)
    )


def _unblast(name2vals, name_map):
    def _collect(names):
        return tuple(name2vals[n] for n in names)

    return {bvname: collect(names) for bvname, names in name_map}


def _named_indexes(wordlen, root):
    return tuple(f"{root}[{i}]" for i in range(wordlen))


class AIGBV(NamedTuple):
    aig: aig.AIG
    input_map: BV_MAP
    output_map: BV_MAP
    latch_map: BV_MAP

    def __rshift__(self, other):
        interface = self.outputs & other.inputs

        assert not self.latches & other.latches
        assert not (self.outputs - interface) & other.outputs

        input_map2 = {kv for kv in other.input_map if kv[0] not in interface}
        output_map2 = {kv for kv in self.output_map if kv[0] not in interface}
        return AIGBV(
            aig=self.aig >> other.aig,
            input_map=self.input_map | input_map2,
            output_map=output_map2 | other.output_map,
            latch_map=self.latch_map | other.latch_map,
        )

    def __or__(self, other):
        assert not self.inputs & other.inputs
        assert not self.outputs & other.outputs
        assert not self.latches & other.latches

        return AIGBV(
            aig=self.aig | other.aig,
            input_map=self.input_map | other.input_map,
            output_map=self.output_map | other.output_map,
            latch_map=self.latch_map | other.latch_map
        )

    def __call__(self, inputs, latches=None):
        if latches is None:
            latches = dict()
        
        out_vals, latch_vals = self.aig(
            inputs=_blast(inputs, self.input_map),
            latches=_blast(latches, self.latch_map)
        )
        outputs = _unblast(out_vals, self.output_map)
        latch_outs = _unblast(latch_vals, self.latch_map)
        return outputs, latch_outs

    @property
    def inputs(self):
        return set(fn.pluck(0, self.input_map))

    @property
    def outputs(self):
        return set(fn.pluck(0, self.output_map))

    @property
    def latches(self):
        return set(fn.pluck(0, self.latch_map))


def bitwise_binop(binop, wordlen, left='x', right='y', output='x&y'):
    lefts = _named_indexes(wordlen, left)
    rights = _named_indexes(wordlen, right)
    outputs = _named_indexes(wordlen, output)

    aig = reduce(
        op.or_,
        (binop([l, r], o) for l, r, o in zip(lefts, rights, outputs))
    )
    return AIGBV(
        aig=aig,
        input_map=frozenset([(left, lefts), (right, rights)]),
        output_map=frozenset([(output, outputs)]),
        latch_map=frozenset(),
    )


def bitwise_and(wordlen, left='x', right='y', output='x&y'):
    return bitwise_binop(common.and_gate, wordlen, left, right, output)


def bitwise_or(wordlen, left='x', right='y', output='x&y'):
    return bitwise_binop(common.or_gate, wordlen, left, right, output)


def bitwise_xor(wordlen, left='x', right='y', output='x&y'):
    return bitwise_binop(common.parity_gate, wordlen, left, right, output)


def bitwise_negate(wordlen, input='x', output='not x'):
    inputs = _named_indexes(wordlen, input)
    outputs = _named_indexes(wordlen, output)
    return AIGBV(
        aig=common.bit_flipper(inputs=inputs, outputs=outputs),
        input_map=frozenset([(input, inputs)]),
        output_map=frozenset([(output, outputs)]),
        latch_map=frozenset(),
    )


def is_nonzero_gate(wordlen, input='x', output='is_nonzero'):
    inputs = _named_indexes(wordlen, input)
    outputs = _named_indexes(1, output)
    return AIGBV(
        aig=common.or_gate(inputs, outputs[0]),
        input_map=frozenset([(input, inputs)]),
        output_map=frozenset([(output, outputs)]),
        latch_map=frozenset(),
    )


def neq_gate(wordlen, left='x', right='y', output='x!=y'):
    return bitwise_xor(wordlen, left, right, left+right) >> \
        is_nonzero_gate(wordlen, left+right, output)


def is_zero_gate(wordlen, input='x', output='is_zero'):
    return is_nonzero_gate(wordlen, input, input+'out') >> \
        bitwise_negate(1, input+'out', output)


def eq_gate(wordlen, left='x', right='y', output='x=y'):
    return neq_gate(wordlen, left, right, left+right) >> \
        bitwise_negate(1, left+right, output)


def source(wordlen, value, name='x', signed=True, byteorder='little'):
    assert 2**wordlen > value
    bits = value.to_bytes(wordlen, byteorder, signed=signed)
    names = _named_indexes(wordlen, name)
    return AIGBV(
        aig=common.source({name: bit for name, bit in zip(names, bits)}),
        input_map=frozenset([(name, names)]),
        output_map=frozenset([(name, names)]),
        latch_map=frozenset(),
    )


def _full_adder(x, y, carry_in, result, carry_out):
    # TODO: Rewrite in aiger. 
    return parser.parse(
        "aag 10 3 0 2 7\n2\n4\n6\n18\n21\n8 4 2\n10 5 3\n"
        "12 11 9\n14 12 6\n16 13 7\n18 17 15\n20 15 9\n"
        f"i0 {x}\ni1 {y}\ni2 {carry_in}\no0 {result}\no1 {carry_out}\n")


def adder(wordlen, output='x+y', left='x', right='y', has_carry=False):
    carry_name = f'{output}_carry'
    assert left != carry_name and right != carry_name

    adder_aig = common.source({carry_name: False})

    lefts = _named_indexes(wordlen, 'left')
    rights = _named_indexes(wordlen, 'right')
    outputs = _named_indexes(wordlen, 'output')

    for lname, rname, oname in zip(lefts, rights, outputs):
        adder_aig >>= _full_adder(
            x=lname,
            y=rname,
            carry_in=carry_name,
            result=oname,
            carry_out=carry_name
        )

    if not has_carry:
        adder_aig >>= common.sink([output + '_carry'])
    
    return AIGBV(
        aig=adder_aig,
        input_map=frozenset([(left, lefts), (right, rights)]),
        output_map=frozenset([(output, outputs)]),
        latch_map=frozenset(),
    )


def subtractor(wordlen, output='x-y', left='x', right='y', has_carry=False):
    return negater(wordlen, right, right) >> \
        adder(wordlen, output, left, right, has_carry)
