from itertools import cycle

from scipy.ndimage import gaussian_filter1d
import numpy as np
from matplotlib import pyplot as plt

import glypy

from .glycan_visual_classification import (
    NGlycanCompositionColorizer, NGlycanCompositionOrderer,
    GlycanLabelTransformer)
from ..chromatogram_tree import ChromatogramInterface, get_chromatogram


def split_charge_states(chromatogram):
    charge_states = chromatogram.charge_states
    versions = {}
    last = chromatogram
    for charge_state in charge_states:
        a, b = last.bisect_charge(charge_state)
        versions[charge_state] = a
        last = b
    return versions


def label_include_charges(chromatogram, *args, **kwargs):
    return "%s-%r" % (
        default_label_extractor(chromatogram, **kwargs),
        tuple(chromatogram.charge_states))


def default_label_extractor(chromatogram, **kwargs):
    if chromatogram.composition:
        return str(chromatogram.composition)
    else:
        return str(chromatogram.neutral_mass)


class ColorCycler(object):
    def __init__(self, colors=None):
        if colors is None:
            colors = ['red', 'green', 'blue', 'yellow', 'purple', 'grey', 'black', "orange"]
        self.color_cycler = cycle(colors)

    def __call__(self, *args, **kwargs):
        return next(self.color_cycler)


class NGlycanChromatogramColorizer(object):
    def __call__(self, chromatogram, default_color='black'):
        if chromatogram.composition is None:
            return default_color
        else:
            try:
                return NGlycanCompositionColorizer(chromatogram.glycan_composition)
            except:
                return default_color


n_glycan_colorizer = NGlycanChromatogramColorizer()


class LabelProducer(object):
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, chromatogram, *args, **kwargs):
        return default_label_extractor(chromatogram)


class NGlycanLabelProducer(LabelProducer):
    def __init__(self, monosaccharides=("HexNAc", "Hex", "Fuc", "NeuAc")):
        self.monosaccharides = monosaccharides
        self.stub = glypy.GlycanComposition()
        for x in monosaccharides:
            self.stub[x] = -99
        self.label_key = GlycanLabelTransformer([self.stub], NGlycanCompositionOrderer).label_key

    def __call__(self, chromatogram, *args, **kwargs):
        if chromatogram.composition is not None:
            return list(GlycanLabelTransformer(
                [chromatogram.glycan_composition, self.stub], NGlycanCompositionOrderer))[0]
        else:
            return chromatogram.key


n_glycan_labeler = NGlycanLabelProducer()


class AbundantLabeler(LabelProducer):
    def __init__(self, labeler, threshold):
        self.labeler = labeler
        self.threshold = threshold

    def __call__(self, chromatogram, *args, **kwargs):
        if chromatogram.total_signal > self.threshold:
            return self.labeler(chromatogram, *args, **kwargs), True
        else:
            return self.labeler(chromatogram, *args, **kwargs), False


class ArtistBase(object):

    def __repr__(self):
        return "{self.__class__.__name__}()".format(self=self)

    def _repr_html_(self):
        if self.ax is None:
            return repr(self)
        fig = (self.ax.get_figure())
        return fig._repr_html_()


class ChromatogramArtist(ArtistBase):
    default_label_function = staticmethod(default_label_extractor)
    include_points = True

    def __init__(self, chromatograms, ax=None, colorizer=None):
        if colorizer is None:
            colorizer = ColorCycler()
        if ax is None:
            fig, ax = plt.subplots(1)

        chromatograms = self._resolve_chromatograms_from_argument(chromatograms)
        chromatograms = [get_chromatogram(c) for c in chromatograms]
        self.chromatograms = chromatograms
        self.minimum_ident_time = float("inf")
        self.maximum_ident_time = 0
        self.maximum_intensity = 0
        self.scan_id_to_intensity = {}
        self.ax = ax
        self.default_colorizer = colorizer
        self.legend = None

    def _resolve_chromatograms_from_argument(self, chromatograms):
        try:
            # if not hasattr(chromatograms[0], "get_chromatogram"):
            if not get_chromatogram(chromatograms[0]):
                chromatograms = [chromatograms]
        except TypeError:
            chromatograms = [chromatograms]
        return chromatograms

    def draw_generic_chromatogram(self, label, rt, heights, color, fill=False):
        if fill:
            s = self.ax.fill_between(
                rt,
                heights,
                alpha=0.25,
                color=color,
                label=label
            )

        else:
            s = self.ax.plot(rt, heights, color=color, label=label, alpha=0.5)[0]

        s.set_gid(str(label) + "-area")
        if self.include_points:
            s = self.ax.scatter(
                rt,
                heights,
                color=color,
                s=1)
            s.set_gid(str(label) + "-points")
        apex = max(heights)
        apex_ind = heights.index(apex)
        rt_apex = rt[apex_ind]

        if label is not None:
            self.ax.text(rt_apex, apex + 1200, label, ha='center', fontsize=10)

    def draw_group(self, label, rt, heights, color, label_peak=True, chromatogram=None):
        if chromatogram is not None:
            try:
                key = str(chromatogram.id)
            except AttributeError:
                key = str(id(chromatogram))
        else:
            key = str(label)

        s = self.ax.fill_between(
            rt,
            heights,
            alpha=0.25,
            color=color,
            label=label
        )
        s.set_gid(key + "-area")
        if self.include_points:
            s = self.ax.scatter(
                rt,
                heights,
                color=color,
                s=1)
            s.set_gid(key + "-points")
        apex = max(heights)
        apex_ind = np.argmax(heights)
        rt_apex = rt[apex_ind]

        if label is not None and label_peak:
            self.ax.text(rt_apex, apex + 1200, label, ha='center', fontsize=10)

    def process_group(self, composition, chromatogram, label_function=None):
        if label_function is None:
            label_function = self.default_label_function
        part = slice(None)
        peaks = chromatogram.peaks[part]
        ids = chromatogram.scan_ids[part]

        color = self.default_colorizer(chromatogram)

        rt, heights = chromatogram.as_arrays()

        self.scan_id_to_intensity = dict(zip(ids, heights))

        self.maximum_ident_time = max(max(rt), self.maximum_ident_time)
        self.minimum_ident_time = min(min(rt), self.minimum_ident_time)

        self.maximum_intensity = max(max(heights), self.maximum_intensity)

        label = label_function(
            chromatogram, rt=rt, heights=heights, peaks=peaks)
        if isinstance(label, basestring):
            label = label
            label_peak = True
        else:
            label, label_peak = label

        self.draw_group(label, rt, heights, color, label_peak, chromatogram)

    def layout_axes(self, legend=True):
        self.ax.set_xlim(self.minimum_ident_time - 0.02,
                         self.maximum_ident_time + 0.02)
        self.ax.set_ylim(0, self.maximum_intensity * 1.1)
        if legend:
            self.legend = self.ax.legend(bbox_to_anchor=(1.7, 1.), ncol=2, fontsize=10)
        self.ax.axes.spines['right'].set_visible(False)
        self.ax.axes.spines['top'].set_visible(False)
        self.ax.yaxis.tick_left()
        self.ax.xaxis.tick_bottom()
        self.ax.set_xlabel("Retention Time", fontsize=28)
        self.ax.set_ylabel("Relative Abundance", fontsize=28)
        [t.set(fontsize=20) for t in self.ax.get_xticklabels()]
        [t.set(fontsize=20) for t in self.ax.get_yticklabels()]

    def draw(self, filter_function=lambda x, y: False, label_function=None,
             legend=True):
        if label_function is None:
            label_function = self.default_label_function
        for chroma in self.chromatograms:
            composition = chroma.composition
            if composition is not None:
                if hasattr(chroma, 'entity') and chroma.entity is not None:
                    gc = chroma.glycan_composition
                else:
                    gc = glypy.GlycanComposition.parse(composition)
            else:
                gc = None
            if filter_function(gc, chroma):
                continue

            self.process_group(composition, chroma, label_function)
        self.layout_axes(legend=legend)
        return self


class SmoothingChromatogramArtist(ChromatogramArtist):
    def __init__(self, chromatograms, ax=None, colorizer=None, smoothing_factor=1.0):
        super(SmoothingChromatogramArtist, self).__init__(chromatograms, ax=ax, colorizer=colorizer)
        self.smoothing_factor = smoothing_factor

    def draw_group(self, label, rt, heights, color, label_peak=True, chromatogram=None):
        if chromatogram is not None:
            try:
                key = str(chromatogram.id)
            except AttributeError:
                key = str(id(chromatogram))
        else:
            key = str(label)
        heights = gaussian_filter1d(heights, self.smoothing_factor)
        s = self.ax.fill_between(
            rt,
            heights,
            alpha=0.25,
            color=color,
            label=label
        )
        s.set_gid(key + "-area")
        s = self.ax.scatter(
            rt,
            heights,
            color=color,
            s=1)
        s.set_gid(key + "-points")
        apex = max(heights)
        apex_ind = np.argmax(heights)
        rt_apex = rt[apex_ind]

        if label is not None and label_peak:
            self.ax.text(rt_apex, apex + 1200, label, ha='center', fontsize=10)

    def draw_generic_chromatogram(self, label, rt, heights, color, fill=False):
        heights = gaussian_filter1d(heights, self.smoothing_factor)
        if fill:
            s = self.ax.fill_between(
                rt,
                heights,
                alpha=0.25,
                color=color,
                label=label
            )

        else:
            s = self.ax.plot(rt, heights, color=color, label=label, alpha=0.5)[0]

        s.set_gid(str(label) + "-area")
        s = self.ax.scatter(
            rt,
            heights,
            color=color,
            s=1)
        s.set_gid(str(label) + "-points")
        apex = max(heights)
        apex_ind = np.argmax(heights)
        rt_apex = rt[apex_ind]

        if label is not None:
            self.ax.text(rt_apex, apex + 1200, label, ha='center', fontsize=10)


class ChargeSeparatingChromatogramArtist(ChromatogramArtist):
    default_label_function = staticmethod(label_include_charges)

    def process_group(self, composition, chroma, label_function=None):
        if label_function is None:
            label_function = self.default_label_function
        charge_state_map = split_charge_states(chroma)
        for charge_state, component in charge_state_map.items():
            super(ChargeSeparatingChromatogramArtist, self).process_group(
                composition, component, label_function=label_function)


class ChargeSeparatingSmoothingChromatogramArtist(
        ChargeSeparatingChromatogramArtist, SmoothingChromatogramArtist):
    pass
