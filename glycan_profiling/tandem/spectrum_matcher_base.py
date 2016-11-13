from collections import defaultdict
from .ref import TargetReference, SpectrumReference
from glypy.composition.glycan_composition import FrozenMonosaccharideResidue


_standard_oxonium_ions = [
    FrozenMonosaccharideResidue.from_iupac_lite("HexNAc"),
    FrozenMonosaccharideResidue.from_iupac_lite("Hex"),
    FrozenMonosaccharideResidue.from_iupac_lite('NeuAc'),
    FrozenMonosaccharideResidue.from_iupac_lite("Fuc")
]


class OxoniumIonScanner(object):
    def __init__(self, ions_to_search=None):
        if ions_to_search is None:
            ions_to_search = _standard_oxonium_ions
        self.ions_to_search = ions_to_search

    def scan(self, peak_list, charge=0, error_tolerance=2e-5):
        matches = []
        for ion in self.ions_to_search:
            match = peak_list.has_peak(ion.mass(charge=charge), error_tolerance)
            if match is not None:
                matches.append(match)
        return matches

    def ratio(self, peak_list, charge=0, error_tolerance=2e-5):
        total = sum(p.intensity for p in peak_list)
        oxonium = sum(p.intensity for p in self.scan(peak_list, charge, error_tolerance))
        return oxonium / total

    def gscore(self, peak_list, charge=0, error_tolerance=2e-5):
        maximum = max(p.intensity for p in peak_list)
        oxonium = sum(p.intensity / maximum for p in self.scan(peak_list, charge, error_tolerance))
        n = len(self.ions_to_search)
        return oxonium / n

    def __call__(self, peak_list, charge=0, error_tolerance=2e-5):
        return self.ratio(peak_list, charge, error_tolerance)


oxonium_detector = OxoniumIonScanner()


def group_by_precursor_mass(scans, window_size=1.5e-5):
    scans = sorted(scans, key=lambda x: x.precursor_information.extracted_neutral_mass)
    groups = []
    current_group = [scans[0]]
    last_scan = scans[0]
    for scan in scans[1:]:
        delta = (scan.precursor_information.extracted_neutral_mass -
                 last_scan.precursor_information.extracted_neutral_mass
                 ) / last_scan.precursor_information.extracted_neutral_mass
        if delta > window_size:
            groups.append(current_group)
            current_group = [scan]
        else:
            current_group.append(scan)
        last_scan = scan
    groups.append(current_group)
    return groups


class SpectrumMatchBase(object):
    def __init__(self, scan, target):
        self.scan = scan
        self.target = target

    def precursor_ion_mass(self):
        neutral_mass = self.scan.precursor_information.extracted_neutral_mass
        return neutral_mass

    def precursor_mass_accuracy(self):
        observed = self.precursor_ion_mass()
        theoretical = self.target.total_composition().mass
        return (observed - theoretical) / theoretical

    def __eq__(self, other):
        try:
            target_id = self.target.id
        except AttributeError:
            target_id = None
        try:
            other_target_id = self.target.id
        except AttributeError:
            other_target_id = None
        return (self.scan == other.scan) and (self.target == other.target) and (
            target_id == other_target_id)

    def __hash__(self):
        try:
            target_id = self.target.id
        except AttributeError:
            target_id = None
        return hash((self.scan.id, self.target, target_id))


class SpectrumMatcherBase(SpectrumMatchBase):
    def __init__(self, scan, target):
        self.scan = scan
        self.spectrum = scan.deconvoluted_peak_set
        self.target = target
        self._score = 0

    @property
    def score(self):
        return self._score

    def match(self, *args, **kwargs):
        raise NotImplementedError()

    def calculate_score(self, *args, **kwargs):
        raise NotImplementedError()

    @classmethod
    def evaluate(cls, scan, target, *args, **kwargs):
        inst = cls(scan, target)
        inst.match(*args, **kwargs)
        inst.calculate_score(*args, **kwargs)
        return inst

    @staticmethod
    def load_peaks(scan):
        try:
            return scan.convert(fitted=False, deconvoluted=True)
        except AttributeError:
            return scan

    def __repr__(self):
        return "{self.__class__.__name__}({self.spectrum}, {self.target}, {self.score})".format(
            self=self)


class DeconvolutingSpectrumMatcherBase(SpectrumMatcherBase):
    @staticmethod
    def load_peaks(scan):
        try:
            return scan.convert(fitted=True, deconvoluted=False)
        except AttributeError:
            return scan


class SpectrumMatch(SpectrumMatchBase):
    def __init__(self, scan, target, score, best_match=False, data_bundle=None):
        if data_bundle is None:
            data_bundle = dict()
        self.scan = scan
        self.target = target
        self.score = score
        self.best_match = best_match
        self.data_bundle = data_bundle
        # self.clear_caches()

    def clear_caches(self):
        try:
            self.target.clear_caches()
        except AttributeError:
            pass

    def evaluate(self, scorer_type, *args, **kwargs):
        if isinstance(self.scan, SpectrumReference):
            raise TypeError("Cannot evaluate a spectrum reference")
        elif isinstance(self.target, TargetReference):
            raise TypeError("Cannot evaluate a target reference")
        return scorer_type.evaluate(self.scan, self.target, *args, **kwargs)

    def __repr__(self):
        return "SpectrumMatch(%s, %s, %0.4f)" % (self.scan, self.target, self.score)

    @classmethod
    def from_match_solution(cls, match):
        return cls(match.scan, match.target, match.score)


class SpectrumSolutionSet(object):
    def __init__(self, scan, solutions):
        self.scan = scan
        # self.oxonium_ratio = oxonium_detector(scan.deconvoluted_peak_set)
        self.solutions = solutions
        self.mean = self._score_mean()
        self.variance = self._score_variance()
        self._is_simplified = False
        self._is_top_only = False

    @property
    def score(self):
        return self.best_solution().score

    def precursor_ion_mass(self):
        neutral_mass = self.scan.precursor_information.extracted_neutral_mass
        return neutral_mass

    def precursor_mass_accuracy(self):
        return self.best_solution().precursor_mass_accuracy()

    def best_solution(self):
        return self.solutions[0]

    def _score_mean(self):
        i = 0
        total = 0
        for match in self:
            total += match.score
            i += 1.
        if i > 0:
            return total / i
        else:
            return 0

    def _score_variance(self):
        total = 0.
        i = 0.
        mean = self.mean
        for match in self:
            total += (match.score - mean) ** 2
            i += 1.
        if i < 3:
            return 0
        return total / (i - 2.)

    def __repr__(self):
        if len(self) == 0:
            return "SpectrumSolutionSet(%s, [])" % (self.scan,)
        return "SpectrumSolutionSet(%s, %s, %f)" % (
            self.scan, self.best_solution().target, self.best_solution().score)

    def __getitem__(self, i):
        return self.solutions[i]

    def __iter__(self):
        return iter(self.solutions)

    def __len__(self):
        return len(self.solutions)

    def threshold(self):
        if len(self) == 0:
            return self
        thresh = min(self.mean / 2., self.score / 2.)
        self.solutions = [
            x for x in self if x.score >= thresh
        ]
        return self

    def simplify(self):
        if self._is_simplified:
            return
        self.scan = SpectrumReference(self.scan.id, self.scan.precursor_information)
        solutions = []
        best_score = self.best_solution().score
        for sol in self.solutions:
            sm = SpectrumMatch.from_match_solution(sol)
            if abs(sm.score - best_score) < 1e-6:
                sm.best_match = True
            sm.scan = self.scan
            solutions.append(sm)
        self.solutions = solutions
        self._is_simplified = True

    def get_top_solutions(self):
        score = self.best_solution().score
        return [x for x in self.solutions if abs(x.score - score) < 1e-6]

    def select_top(self):
        if self._is_top_only:
            return
        self.solutions = self.get_top_solutions()
        self._is_top_only = True


class TandemClusterEvaluatorBase(object):
    def __init__(self, tandem_cluster, scorer_type, structure_database):
        self.tandem_cluster = tandem_cluster
        self.scorer_type = scorer_type
        self.structure_database = structure_database

    def score_one(self, scan, precursor_error_tolerance=1e-5, *args, **kwargs):
        solutions = []

        hits = self.structure_database.search_mass_ppm(
            scan.precursor_information.extracted_neutral_mass,
            precursor_error_tolerance)

        for structure in hits:
            result = self.evaluate(scan, structure, *args, **kwargs)
            solutions.append(result)
        out = SpectrumSolutionSet(
            scan, sorted(
                solutions, key=lambda x: x.score, reverse=True)).threshold()
        return out

    def score_all(self, precursor_error_tolerance=1e-5, simplify=False, *args, **kwargs):
        out = []
        for scan in self.tandem_cluster:
            solutions = self.score_one(scan, precursor_error_tolerance, *args, **kwargs)
            if len(solutions) > 0:
                out.append(solutions)
        if simplify:
            for case in out:
                case.simplify()
                case.select_top()
        return out

    def evaluate(self, scan, structure, *args, **kwargs):
        raise NotImplementedError()

    def score_bunch(self, scans, precursor_error_tolerance=1e-5, *args, **kwargs):
        groups = group_by_precursor_mass(scans, precursor_error_tolerance * 1.5)

        hit_to_scan = defaultdict(list)
        scan_map = {}
        hit_map = {}
        for group in groups:
            for scan in group:
                scan_map[scan.id] = scan
                for hit in self.structure_database.search_mass_ppm(
                        scan.precursor_information.extracted_neutral_mass,
                        precursor_error_tolerance):
                    hit_to_scan[hit.id].append(scan)
                    hit_map[hit.id] = hit

        scan_solution_map = defaultdict(list)
        for hit_id, scan_list in hit_to_scan.items():
            hit = hit_map[hit_id]
            solutions = []
            for scan in scan_list:
                match = SpectrumMatch.from_match_solution(self.evaluate(scan, hit, *args, **kwargs))
                scan_solution_map[scan.id].append(match)
                solutions.append(match)
            # Assumes all matches to the same target structure share a cache
            match.clear_caches()
            self.reset_parser()

        result_set = []
        for scan_id, solutions in scan_solution_map.items():
            scan = scan_map[scan_id]
            out = SpectrumSolutionSet(scan, sorted(
                solutions, key=lambda x: x.score, reverse=True)).threshold()
            result_set.append(out)

        return result_set
