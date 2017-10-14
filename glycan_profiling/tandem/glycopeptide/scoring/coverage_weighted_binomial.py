import math

from .binomial_score import BinomialSpectrumMatcher
from .simple_score import SimpleCoverageScorer
from .precursor_mass_accuracy import MassAccuracyModel
from glycan_profiling.structure import FragmentMatchMap


accuracy_bias = MassAccuracyModel(-2.673807e-07, 5.022458e-06)


class CoverageWeightedBinomialScorer(BinomialSpectrumMatcher, SimpleCoverageScorer):
    def __init__(self, scan, sequence):
        BinomialSpectrumMatcher.__init__(self, scan, sequence)
        self.glycosylated_b_ion_count = 0
        self.glycosylated_y_ion_count = 0

    def match(self, error_tolerance=2e-5, *args, **kwargs):
        solution_map = FragmentMatchMap()
        spectrum = self.spectrum
        n_theoretical = 0
        backbone_mass_series = []
        neutral_losses = tuple(kwargs.pop("neutral_losses", []))

        oxonium_ion_matches = set()
        for frag in self.target.glycan_fragments(
                all_series=False, allow_ambiguous=False,
                include_large_glycan_fragments=False,
                maximum_fragment_size=4):
            peak = spectrum.has_peak(frag.mass, error_tolerance)
            if peak:
                solution_map.add(peak, frag)
                oxonium_ion_matches.add(peak)
                try:
                    self._sanitized_spectrum.remove(peak)
                except KeyError:
                    continue

        n_glycosylated_b_ions = 0
        for frags in self.target.get_fragments('b', neutral_losses):
            glycosylated_position = False
            n_theoretical += 1
            for frag in frags:
                backbone_mass_series.append(frag)
                glycosylated_position |= frag.is_glycosylated
                for peak in spectrum.all_peaks_for(frag.mass, error_tolerance):
                    if peak in oxonium_ion_matches:
                        continue
                    solution_map.add(peak, frag)
            if glycosylated_position:
                n_glycosylated_b_ions += 1

        n_glycosylated_y_ions = 0
        for frags in self.target.get_fragments('y', neutral_losses):
            glycosylated_position = False
            n_theoretical += 1
            for frag in frags:
                backbone_mass_series.append(frag)
                glycosylated_position |= frag.is_glycosylated
                for peak in spectrum.all_peaks_for(frag.mass, error_tolerance):
                    if peak in oxonium_ion_matches:
                        continue
                    solution_map.add(peak, frag)
            if glycosylated_position:
                n_glycosylated_y_ions += 1

        for frag in self.target.stub_fragments(extended=True):
            for peak in spectrum.all_peaks_for(frag.mass, error_tolerance):
                solution_map.add(peak, frag)

        self.n_theoretical = n_theoretical
        self.glycosylated_b_ion_count = n_glycosylated_b_ions
        self.glycosylated_y_ion_count = n_glycosylated_y_ions
        self.solution_map = solution_map
        self._backbone_mass_series = backbone_mass_series
        return solution_map

    def calculate_score(self, match_tolerance=2e-5, *args, **kwargs):
        bin_score = BinomialSpectrumMatcher.calculate_score(
            self, match_tolerance=match_tolerance)
        coverage_score = SimpleCoverageScorer.calculate_score(self)
        mass_accuracy = -10 * math.log10(
            1 - accuracy_bias.score(self.precursor_mass_accuracy()))
        self._score = bin_score * coverage_score + mass_accuracy
        return self._score
