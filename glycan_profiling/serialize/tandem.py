from weakref import WeakValueDictionary

from sqlalchemy import (
    Column, Numeric, Integer, String, ForeignKey, PickleType,
    Boolean, Table)
from sqlalchemy.orm import relationship, backref, object_session
from sqlalchemy.ext.declarative import declared_attr

from glycan_profiling.tandem.spectrum_matcher_base import (
    SpectrumMatch as MemorySpectrumMatch, SpectrumSolutionSet as MemorySpectrumSolutionSet, SpectrumReference,
    TargetReference)

from .analysis import BoundToAnalysis
from .hypothesis import Glycopeptide, GlycanComposition

from .base import (
    Base, MSScan)


class SpectrumMatchBase(BoundToAnalysis):

    score = Column(Numeric(12, 6, asdecimal=False), index=True)

    @declared_attr
    def scan_id(self):
        return Column(Integer, ForeignKey(MSScan.id), index=True)

    @declared_attr
    def scan(self):
        return relationship(MSScan)


class SpectrumClusterBase(object):
    def __getitem__(self, i):
        return self.spectrum_solutions[i]

    def __iter__(self):
        return iter(self.spectrum_solutions)

    def convert(self):
        return [x.convert() for x in self.spectrum_solutions]


class SolutionSetBase(object):

    @declared_attr
    def scan_id(self):
        return Column(Integer, ForeignKey(MSScan.id), index=True)

    @declared_attr
    def scan(self):
        return relationship(MSScan)

    @property
    def scan_time(self):
        return self.scan.scan_time

    def best_solution(self):
        return sorted(self.spectrum_matches, key=lambda x: x.score, reverse=True)[0]

    @property
    def score(self):
        return self.best_solution().score

    def __getitem__(self, i):
        return self.spectrum_matches[i]

    def __iter__(self):
        return iter(self.spectrum_matches)

    _target_map = None

    def _make_target_map(self):
        self._target_map = {
            sol.target: sol for sol in self
        }

    def solution_for(self, target):
        if self._target_map is None:
            self._make_target_map()
        return self._target_map[target]


class GlycopeptideSpectrumCluster(Base, SpectrumClusterBase, BoundToAnalysis):
    __tablename__ = "GlycopeptideSpectrumCluster"

    id = Column(Integer, primary_key=True)

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, *args, **kwargs):
        inst = cls()
        session.add(inst)
        session.flush()
        cluster_id = inst.id
        for solution_set in obj.tandem_solutions:
            GlycopeptideSpectrumSolutionSet.serialize(
                solution_set, session, scan_look_up_cache, analysis_id,
                cluster_id, *args, **kwargs)
        return inst


class GlycopeptideSpectrumSolutionSet(Base, SolutionSetBase, BoundToAnalysis):
    __tablename__ = "GlycopeptideSpectrumSolutionSet"

    id = Column(Integer, primary_key=True)
    cluster_id = Column(
        Integer,
        ForeignKey(GlycopeptideSpectrumCluster.id, ondelete="CASCADE"),
        index=True)

    cluster = relationship(GlycopeptideSpectrumCluster, backref=backref("spectrum_solutions", lazy='subquery'))

    is_decoy = Column(Boolean, index=True)

    # scan_id = Column(Integer, ForeignKey(MSScan.id), index=True)
    # scan = relationship(MSScan)

    # def best_solution(self):
    #     return sorted(self.spectrum_matches, key=lambda x: x.score, reverse=True)[0]

    # @property
    # def score(self):
    #     return self.best_solution().score

    # def __iter__(self):
    #     return iter(self.spectrum_matches)

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, cluster_id, is_decoy=False, *args, **kwargs):
        inst = cls(
            scan_id=scan_look_up_cache[obj.scan.id],
            is_decoy=is_decoy,
            analysis_id=analysis_id,
            cluster_id=cluster_id)
        session.add(inst)
        session.flush()
        for solution in obj:
            GlycopeptideSpectrumMatch.serialize(
                solution, session, scan_look_up_cache,
                analysis_id, inst.id, is_decoy, *args, **kwargs)
        return inst

    def convert(self):
        matches = [x.convert() for x in self.spectrum_matches]
        matches.sort(key=lambda x: x.score, reverse=True)
        inst = MemorySpectrumSolutionSet(
            SpectrumReference(self.scan.scan_id, self.scan.precursor_information),
            matches
        )
        inst.q_value = min(x.q_value for x in inst)
        inst.id = self.id
        return inst

    def __repr__(self):
        return "DB" + repr(self.convert())


class GlycopeptideSpectrumMatch(Base, SpectrumMatchBase):
    __tablename__ = "GlycopeptideSpectrumMatch"

    id = Column(Integer, primary_key=True)
    solution_set_id = Column(
        Integer, ForeignKey(
            GlycopeptideSpectrumSolutionSet.id, ondelete='CASCADE'),
        index=True)
    solution_set = relationship(GlycopeptideSpectrumSolutionSet, backref=backref("spectrum_matches", lazy='subquery'))
    q_value = Column(Numeric(8, 7, asdecimal=False), index=True)
    is_decoy = Column(Boolean, index=True)
    is_best_match = Column(Boolean, index=True)

    structure_id = Column(
        Integer, ForeignKey(Glycopeptide.id, ondelete='CASCADE'),
        index=True)

    structure = relationship(Glycopeptide)

    @property
    def target(self):
        return self.structure

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id,
                  solution_set_id, is_decoy=False, *args, **kwargs):
        inst = cls(
            scan_id=scan_look_up_cache[obj.scan.id],
            is_decoy=is_decoy,
            analysis_id=analysis_id,
            score=obj.score,
            q_value=obj.q_value,
            solution_set_id=solution_set_id,
            is_best_match=obj.best_match,
            structure_id=obj.target.id)
        session.add(inst)
        session.flush()
        return inst

    def convert(self):
        session = object_session(self)
        scan = session.query(MSScan).get(self.scan_id).convert()
        target = session.query(Glycopeptide).get(self.structure_id).convert()
        inst = MemorySpectrumMatch(scan, target, self.score, self.is_best_match)
        inst.q_value = self.q_value
        inst.id = self.id
        return inst

    def __repr__(self):
        return "DB" + repr(self.convert())


class GlycanCompositionSpectrumCluster(Base, SpectrumClusterBase, BoundToAnalysis):
    __tablename__ = "GlycanCompositionSpectrumCluster"

    id = Column(Integer, primary_key=True)

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, *args, **kwargs):
        inst = cls()
        session.add(inst)
        session.flush()
        cluster_id = inst.id
        for solution_set in obj.tandem_solutions:
            GlycanCompositionSpectrumSolutionSet.serialize(
                solution_set, session, scan_look_up_cache, analysis_id,
                cluster_id, *args, **kwargs)
        return inst

    source = relationship(
        "GlycanCompositionChromatogram",
        secondary=lambda: GlycanCompositionChromatogramToGlycanCompositionSpectrumCluster,
        backref=backref("spectrum_cluster", uselist=False))


class GlycanCompositionSpectrumSolutionSet(Base, SolutionSetBase, BoundToAnalysis):
    __tablename__ = "GlycanCompositionSpectrumSolutionSet"

    id = Column(Integer, primary_key=True)
    cluster_id = Column(
        Integer,
        ForeignKey(GlycanCompositionSpectrumCluster.id, ondelete="CASCADE"),
        index=True)

    cluster = relationship(GlycanCompositionSpectrumCluster, backref=backref(
        "spectrum_solutions", lazy='subquery'))

    # scan_id = Column(Integer, ForeignKey(MSScan.id), index=True)
    # scan = relationship(MSScan)

    # def best_solution(self):
    #     return sorted(self.spectrum_matches, key=lambda x: x.score, reverse=True)[0]

    # @property
    # def score(self):
    #     return self.best_solution().score

    # def __iter__(self):
    #     return iter(self.spectrum_matches)

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, cluster_id, *args, **kwargs):
        inst = cls(
            scan_id=scan_look_up_cache[obj.scan.id],
            analysis_id=analysis_id,
            cluster_id=cluster_id)
        session.add(inst)
        session.flush()
        # if we have a real SpectrumSolutionSet, then it will be iterable
        try:
            list(obj)
        except TypeError:
            # otherwise we have a single SpectrumMatch
            obj = [obj]
        for solution in obj:
            GlycanCompositionSpectrumMatch.serialize(
                solution, session, scan_look_up_cache,
                analysis_id, inst.id, *args, **kwargs)
        return inst

    def convert(self):
        matches = [x.convert() for x in self.spectrum_matches]
        matches.sort(key=lambda x: x.score, reverse=True)
        inst = MemorySpectrumSolutionSet(
            SpectrumReference(self.scan.scan_id, self.scan.precursor_information),
            matches
        )
        inst.q_value = min(x.q_value for x in inst)
        inst.id = self.id
        return inst

    def __repr__(self):
        return "DB" + repr(self.convert())


class GlycanCompositionSpectrumMatch(Base, SpectrumMatchBase):
    __tablename__ = "GlycanCompositionSpectrumMatch"

    id = Column(Integer, primary_key=True)
    solution_set_id = Column(
        Integer, ForeignKey(
            GlycanCompositionSpectrumSolutionSet.id, ondelete='CASCADE'),
        index=True)
    solution_set = relationship(GlycanCompositionSpectrumSolutionSet,
                                backref=backref("spectrum_matches", lazy='subquery'))

    composition_id = Column(
        Integer, ForeignKey(GlycanComposition.id, ondelete='CASCADE'),
        index=True)

    composition = relationship(GlycanComposition)

    @property
    def target(self):
        return self.composition

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, solution_set_id,
                  is_decoy=False, *args, **kwargs):
        inst = cls(
            scan_id=scan_look_up_cache[obj.scan.id],
            analysis_id=analysis_id,
            score=obj.score,
            solution_set_id=solution_set_id,
            composition_id=obj.target.id)
        session.add(inst)
        session.flush()
        return inst

    def convert(self):
        session = object_session(self)
        scan = session.query(MSScan).get(self.scan_id).convert()
        target = session.query(GlycanComposition).get(self.composition_id).convert()
        inst = MemorySpectrumMatch(scan, target, self.score)
        inst.id = self.id
        return inst

    def __repr__(self):
        return "DB" + repr(self.convert())


class UnidentifiedSpectrumCluster(Base, SpectrumClusterBase, BoundToAnalysis):
    __tablename__ = "UnidentifiedSpectrumCluster"

    id = Column(Integer, primary_key=True)

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, *args, **kwargs):
        inst = cls()
        session.add(inst)
        session.flush()
        cluster_id = inst.id
        for solution_set in obj.tandem_solutions:
            UnidentifiedSpectrumSolutionSet.serialize(
                solution_set, session, scan_look_up_cache, analysis_id,
                cluster_id, *args, **kwargs)
        return inst

    source = relationship(
        "UnidentifiedChromatogram",
        secondary=lambda: UnidentifiedChromatogramToUnidentifiedSpectrumCluster,
        backref=backref("spectrum_cluster", uselist=False))


class UnidentifiedSpectrumSolutionSet(Base, SolutionSetBase, BoundToAnalysis):
    __tablename__ = "UnidentifiedSpectrumSolutionSet"

    id = Column(Integer, primary_key=True)
    cluster_id = Column(
        Integer,
        ForeignKey(UnidentifiedSpectrumCluster.id, ondelete="CASCADE"),
        index=True)

    cluster = relationship(UnidentifiedSpectrumCluster, backref=backref(
        "spectrum_solutions", lazy='subquery'))

    # scan_id = Column(Integer, ForeignKey(MSScan.id), index=True)
    # scan = relationship(MSScan)

    # def best_solution(self):
    #     return sorted(self.spectrum_matches, key=lambda x: x.score, reverse=True)[0]

    # @property
    # def score(self):
    #     return self.best_solution().score

    # def __iter__(self):
    #     return iter(self.spectrum_matches)

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, cluster_id, *args, **kwargs):
        inst = cls(
            scan_id=scan_look_up_cache[obj.scan.id],
            analysis_id=analysis_id,
            cluster_id=cluster_id)
        session.add(inst)
        session.flush()
        # if we have a real SpectrumSolutionSet, then it will be iterable
        try:
            list(obj)
        except TypeError:
            # otherwise we have a single SpectrumMatch
            obj = [obj]
        for solution in obj:
            UnidentifiedSpectrumMatch.serialize(
                solution, session, scan_look_up_cache,
                analysis_id, inst.id, *args, **kwargs)
        return inst

    def convert(self):
        matches = [x.convert() for x in self.spectrum_matches]
        matches.sort(key=lambda x: x.score, reverse=True)
        inst = MemorySpectrumSolutionSet(
            SpectrumReference(self.scan.scan_id, self.scan.precursor_information),
            matches
        )
        inst.q_value = min(x.q_value for x in inst)
        inst.id = self.id
        return inst

    def __repr__(self):
        return "DB" + repr(self.convert())


class UnidentifiedSpectrumMatch(Base, SpectrumMatchBase):
    __tablename__ = "UnidentifiedSpectrumMatch"

    id = Column(Integer, primary_key=True)
    solution_set_id = Column(
        Integer, ForeignKey(
            UnidentifiedSpectrumSolutionSet.id, ondelete='CASCADE'),
        index=True)

    solution_set = relationship(UnidentifiedSpectrumSolutionSet,
                                backref=backref("spectrum_matches", lazy='subquery'))

    @classmethod
    def serialize(cls, obj, session, scan_look_up_cache, analysis_id, solution_set_id,
                  is_decoy=False, *args, **kwargs):
        inst = cls(
            scan_id=scan_look_up_cache[obj.scan.id],
            analysis_id=analysis_id,
            score=obj.score,
            solution_set_id=solution_set_id)
        session.add(inst)
        session.flush()
        return inst

    def convert(self):
        session = object_session(self)
        scan = session.query(MSScan).get(self.scan_id).convert()
        inst = MemorySpectrumMatch(scan, None, self.score)
        inst.id = self.id
        return inst

    def __repr__(self):
        return "DB" + repr(self.convert())


GlycanCompositionChromatogramToGlycanCompositionSpectrumCluster = Table(
    "GlycanCompositionChromatogramToGlycanCompositionSpectrumCluster", Base.metadata,
    Column("chromatogram_id", Integer, ForeignKey(
        "GlycanCompositionChromatogram.id", ondelete="CASCADE"), primary_key=True),
    Column("cluster_id", Integer, ForeignKey(
        GlycanCompositionSpectrumCluster.id, ondelete="CASCADE"), primary_key=True))


UnidentifiedChromatogramToUnidentifiedSpectrumCluster = Table(
    "UnidentifiedChromatogramToUnidentifiedSpectrumCluster", Base.metadata,
    Column("chromatogram_id", Integer, ForeignKey(
        "UnidentifiedChromatogram.id", ondelete="CASCADE"), primary_key=True),
    Column("cluster_id", Integer, ForeignKey(
        UnidentifiedSpectrumCluster.id, ondelete="CASCADE"), primary_key=True))
