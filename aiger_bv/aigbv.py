import re

import aiger
import attr
import funcy as fn
from pyrsistent import pmap

from aiger_bv import common
from aiger_bv.bundle import BundleMap


@attr.s(frozen=True, slots=True, eq=False, auto_attribs=True)
class AIGBV:
    aig: aiger.AIG
    imap: BundleMap = BundleMap()
    omap: BundleMap = BundleMap()
    lmap: BundleMap = BundleMap()

    simulate = aiger.AIG.simulate
    simulator = aiger.AIG.simulator

    def write(self, path):
        self.aig.write(path)

    @property
    def inputs(self): return set(self.imap.keys())

    @property
    def outputs(self): return set(self.omap.keys())

    @property
    def latches(self): return set(self.lmap.keys())

    @property
    def latch2init(self):
        return self.lmap.unblast(dict(self.aig.latch2init))

    def __call__(self, inputs, latches=None):
        out2val, latch2val = self.aig(
            inputs=self.imap.blast(inputs),
            latches=None if latches is None else self.lmap.blast(latches)
        )
        return self.omap.unblast(out2val), self.lmap.unblast(latch2val)

    def __lshift__(self, other):
        return other >> self

    def __rshift__(self, other):
        interface = self.outputs & other.inputs
        assert not self.latches & other.latches
        assert not (self.outputs - interface) & other.outputs

        return AIGBV(
            aig=self.aig >> other.aig,
            imap=self.imap + other.imap.omit(interface),
            omap=other.omap + self.omap.omit(interface),
            lmap=self.lmap + other.lmap,
        )

    def __or__(self, other):
        assert not self.outputs & other.outputs
        assert not self.latches & other.latches

        shared_inputs = self.inputs & other.inputs
        if shared_inputs:
            relabels1 = {n: common._fresh() for n in shared_inputs}
            relabels2 = {n: common._fresh() for n in shared_inputs}
            self, other = self['i', relabels1], other['i', relabels2]

        circ = AIGBV(
            aig=self.aig | other.aig,
            imap=self.imap + other.imap,
            omap=self.omap + other.omap,
            lmap=self.lmap + other.lmap)

        if shared_inputs:
            for orig in shared_inputs:
                new1, new2 = relabels1[orig], relabels2[orig]
                circ <<= common.tee(len(self.imap[orig]), {orig: [new1, new2]})

        return circ

    def __getitem__(self, others):
        kind, relabels = others
        if kind not in {'i', 'o', 'l'}:
            raise NotImplementedError

        attr_name = {'i': 'imap', 'o': 'omap', 'l': 'lmap'}.get(kind)
        bmap1 = getattr(self, attr_name)
        assert not set(relabels.values()) & set(bmap1.keys())
        bmap2 = bmap1.relabel(relabels)
        circ = attr.evolve(self, **{attr_name: bmap2})

        # Update AIG to match new interface.
        relabels_aig = fn.merge(*(
            dict(zip(bmap1[k], bmap2[v])) for k, v in relabels.items()
            if k in bmap1
        ))
        return attr.evolve(circ, aig=circ.aig[kind, relabels_aig])

    def feedback(self, inputs, outputs, initials=None, latches=None,
                 keep_outputs=False):
        if latches is None:
            latches = inputs

        def blast(bmap, vals):
            return fn.lmapcat(bmap.get, vals)

        lmap = BundleMap(
            {l: self.imap[i].size for i, l in zip(inputs, latches)}
        )
        aig = rebundle_aig(self.aig.feedback(
            inputs=blast(self.imap, inputs), outputs=blast(self.omap, outputs),
            latches=blast(lmap, latches), keep_outputs=keep_outputs,
        ))

        if initials is not None:
            l2init = dict(aig.latch2init).update(
                {l: v for l, v in zip(latches, initials) if v is not None}
            )
            initials = fn.lcat(l2init[l] for l in latches)

        return aig

    def unroll(self, horizon, *, init=True, omit_latches=True,
               only_last_outputs=False):
        aig = self.aig.unroll(
            horizon, init=init, omit_latches=omit_latches,
            only_last_outputs=only_last_outputs
        )
        for key in ['inputs', 'outputs', 'latches']:
            relabels = {k: shuffle_id_time(k) for k in getattr(aig, key)}
            aig = aig[key[0], relabels]

        return rebundle_aig(aig)


# Lifting AIGs to AIGBVs

def _diagonal_map(keys):
    return BundleMap({k: 1 for k in keys})


def append_index(aig):
    for key in ['inputs', 'outputs', 'latches']:
        relabels = {name: f"{name}[0]" for name in getattr(aig, key)}
        aig = aig[key[0], relabels]
    return aig


def aig2aigbv(aig):
    return AIGBV(
        aig=append_index(aig),
        imap=_diagonal_map(aig.inputs),
        omap=_diagonal_map(aig.outputs),
        lmap=_diagonal_map(aig.latches),
    )


BV_NAME = re.compile(r"(.*)\[(\d+)\]$")


def unpack_name(name):
    root, idx = BV_NAME.match(name).groups()
    return root, int(idx)


def to_size(idxs):
    idxs2 = set(idxs)
    assert len(idxs2) == len(idxs)
    assert min(idxs2) == 0
    assert max(idxs2) == len(idxs) - 1
    return len(idxs)


def rebundle_names(names):
    grouped_names = fn.group_values(map(unpack_name, names))
    return BundleMap(pmap(fn.walk_values(to_size, grouped_names)))


def rebundle_aig(aig):
    return AIGBV(
        aig=aig,
        imap=rebundle_names(aig.inputs),
        omap=rebundle_names(aig.outputs),
        lmap=rebundle_names(aig.latches),
    )


# For relabeling time unrolling
BV_NAME_TIME = re.compile(r"(.*)\[(\d+)\]##time_(\d+)$")


def shuffle_id_time(name):
    name, idx, time = BV_NAME_TIME.match(name).groups()
    return f"{name}##time_{time}[{idx}]"