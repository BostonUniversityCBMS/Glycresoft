# -*- coding: utf-8 -*-

'''
Much of this logic is derived from:

    Risk, B. A., Edwards, N. J., & Giddings, M. C. (2013). A peptide-spectrum scoring system
    based on ion alignment, intensity, and pair probabilities. Journal of Proteome Research,
    12(9), 4240–7. http://doi.org/10.1021/pr400286p
'''


import numpy as np
from scipy.misc import comb

from ms_peak_picker.utils import draw_peaklist

from ...spectrum_matcher_base import SpectrumMatcherBase
from ...spectrum_annotation import annotate_matched_deconvoluted_peaks
from glycresoft_sqlalchemy.utils.memoize import memoize


@memoize(10000)
def binomial_tail_probability(n, k, p):
    total = 0.0
    for i in range(k, n):
        v = comb(n, i, exact=True) * (p ** i) * ((1 - p) ** (n - i))
        if np.isnan(v):
            continue
        total += v
    return total


def binomial_fragments_matched(total_product_ion_count, count_product_ion_matches, ion_tolerance,
                               precursor_mass):
    p = np.exp((np.log(ion_tolerance) + np.log(2)) +
               np.log(count_product_ion_matches) - np.log(precursor_mass))
    return binomial_tail_probability(total_product_ion_count, count_product_ion_matches, p)


def median_sorted(numbers):
    n = len(numbers)
    if n % 2 == 0:
        return (n - 1) / 2, (numbers[(n - 1) / 2] + numbers[((n - 1) / 2) + 1]) / 2.
    else:
        return (n - 1) / 2, numbers[(n - 1) / 2]


def medians(array):
    array.sort()
    offset, m1 = median_sorted(array)
    offset += 1
    i, m2 = median_sorted(array[offset:])
    offset += i + 1
    i, m3 = median_sorted(array[offset:])
    offset += i + 1
    i, m4 = median_sorted(array[offset:])
    return m1, m2, m3, m4


def binomial_intensity(peak_list, matched_peaks, total_product_ion_count):
    if len(matched_peaks) == 0:
        return np.exp(0)
    intensity_list = np.array([p.intensity for p in peak_list])
    m1, m2, m3, m4 = medians(intensity_list)

    matched_intensities = np.array(
        [p.intensity for match, p in matched_peaks.items()])
    counts = dict()
    last_count = total_product_ion_count
    next_count = (matched_intensities > m1).sum()
    counts[1] = binomial_tail_probability(last_count, next_count, 0.5)
    last_count = next_count

    next_count = (matched_intensities > m2).sum()
    counts[2] = binomial_tail_probability(last_count, next_count, 0.5)
    last_count = next_count

    next_count = (matched_intensities > m3).sum()
    counts[3] = binomial_tail_probability(last_count, next_count, 0.5)
    last_count = next_count

    next_count = (matched_intensities > m4).sum()
    counts[4] = binomial_tail_probability(last_count, next_count, 0.5)

    prod = 0
    for v in counts.values():
        if v == 0:
            continue
        prod += np.log(v)
    return np.exp(prod)


def calculate_precursor_mass(spectrum_match):
    precursor_mass = spectrum_match.sequence.peptide_composition().mass
    return precursor_mass


class BinomialSpectrumMatcher(SpectrumMatcherBase):

    def __init__(self, scan, sequence):
        super(BinomialSpectrumMatcher, self).__init__(scan, sequence)
        self._sanitized_spectrum = set(self.spectrum)
        self._score = None
        self.solution_map = dict()
        self.n_theoretical = 0

    @property
    def sequence(self):
        return self.target

    @sequence.setter
    def sequence(self, value):
        self.target = value

    def match(self, error_tolerance=2e-5):
        n_theoretical = 0
        solution_map = {}
        spectrum = self.spectrum
        for frag in self.sequence.glycan_fragments(
                all_series=False, allow_ambiguous=False,
                include_large_glycan_fragments=False,
                maximum_fragment_size=4):
            peak = spectrum.has_peak(frag.mass, error_tolerance)
            # n_theoretical += 1
            if peak:
                solution_map[frag] = peak
                try:
                    self._sanitized_spectrum.remove(peak)
                except KeyError:
                    continue
        for frags in self.sequence.get_fragments('b'):
            for frag in frags:
                n_theoretical += 1
                peak = spectrum.has_peak(frag.mass, error_tolerance)
                if peak:
                    solution_map[frag] = peak
        for frags in self.sequence.get_fragments('y'):
            for frag in frags:
                n_theoretical += 1
                peak = spectrum.has_peak(frag.mass, error_tolerance)
                if peak:
                    solution_map[frag] = peak
        for frag in self.sequence.stub_fragments(extended=True):
            n_theoretical += 1
            peak = spectrum.has_peak(frag.mass, error_tolerance)
            if peak:
                solution_map[frag] = peak
        self.solution_map = solution_map
        self.n_theoretical = n_theoretical
        return solution_map

    def _sanitize_solution_map(self):
        san = dict(self.solution_map)
        for k in self.solution_map:
            if hasattr(k, 'kind') and k.kind == "oxonium_ion":
                san.pop(k)
        return san

    def _fragment_matched_binomial(self, match_tolerance=2e-5):
        precursor_mass = calculate_precursor_mass(self)

        fragment_match_component = binomial_fragments_matched(
            self.n_theoretical,
            len(self._sanitize_solution_map()),
            match_tolerance,
            precursor_mass
        )
        return fragment_match_component

    def _intensity_component_binomial(self):
        intensity_component = binomial_intensity(
            self._sanitized_spectrum,
            self._sanitize_solution_map(),
            self.n_theoretical)

        if intensity_component == 0:
            intensity_component = 1e-170
        return intensity_component

    def _binomial_score(self, match_tolerance=2e-5, *args, **kwargs):
        precursor_mass = calculate_precursor_mass(self)

        fragment_match_component = binomial_fragments_matched(
            self.n_theoretical,
            len(self._sanitize_solution_map()),
            match_tolerance,
            precursor_mass
        )

        intensity_component = binomial_intensity(
            self._sanitized_spectrum,
            self._sanitize_solution_map(),
            self.n_theoretical)

        if intensity_component == 0:
            intensity_component = 1e-170
        score = -np.log10(intensity_component) + - \
            np.log10(fragment_match_component)

        if np.isinf(score):
            print "infinite score", intensity_component, fragment_match_component

        return score

    def calculate_score(self, match_tolerance=2e-5, *args, **kwargs):
        score = self._binomial_score(match_tolerance)
        self._score = score
        return score

    def annotate(self, **kwargs):
        ax = draw_peaklist(self.spectrum, alpha=0.3, color='grey', **kwargs)
        draw_peaklist(self._sanitized_spectrum, color='grey', ax=ax, alpha=0.5, **kwargs)
        annotate_matched_deconvoluted_peaks(self.solution_map.items(), ax)
        return draw_peaklist(
            sorted(self.solution_map.values(), key=lambda x: x.neutral_mass), ax=ax, color='red', **kwargs)
