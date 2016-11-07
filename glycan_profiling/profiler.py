import re
import time
import warnings
import logging

import glypy

from glycan_profiling.plotting.summaries import GlycanChromatographySummaryGraphBuilder
from glycan_profiling.database import build_database
from glycan_profiling.database.disk_backed_database import (
    GlycanCompositionDiskBackedStructureDatabase,
    GlycopeptideDiskBackedStructureDatabase)

from glycan_profiling.serialize import (
    DatabaseScanDeserializer, AnalysisSerializer,
    PrecursorInformation, AnalysisTypeEnum)

from glycan_profiling.piped_deconvolve import (
    ScanGenerator as PipedScanGenerator, MzMLLoader)

from glycan_profiling.scoring import (
    ChromatogramSolution, NetworkScoreDistributor, ChromatogramScorer)

from glycan_profiling.chromatogram_tree import (
    ChromatogramOverlapSmoother, GlycanCompositionChromatogram, ChromatogramFilter)

from glycan_profiling.trace import (
    IncludeUnmatchedTracer, join_mass_shifted,
    reverse_adduction_search, prune_bad_adduct_branches,
    NonAggregatingTracer, ScanSink, ChromatogramExtractor,
    ChromatogramProcessor)


from glycan_profiling.tandem import chromatogram_mapping
from glycan_profiling.tandem.glycopeptide.scoring.binomial_score import BinomialSpectrumMatcher
from glycan_profiling.tandem.glycopeptide.glycopeptide_matcher import GlycopeptideDatabaseSearchIdentifier
from glycan_profiling.tandem.glycopeptide import (
    identified_structure as identified_glycopeptide)


from glycan_profiling.scan_cache import (
    NullScanCacheHandler, DatabaseScanCacheHandler,
    DatabaseScanGenerator)

from glycan_profiling.task import TaskBase

from brainpy import periodic_table
from ms_deisotope.averagine import Averagine, glycan as n_glycan_averagine


logger = logging.getLogger("chromatogram_profiler")


def validate_element(element):
    valid = element in periodic_table
    if not valid:
        warnings.warn("%r is not a valid element" % element)
    return valid


def parse_averagine_formula(formula):
    return Averagine({k: float(v) for k, v in re.findall(r"([A-Z][a-z]*)([0-9\.]*)", formula)
                      if float(v or 0) > 0 and validate_element(k)})


class SampleConsumer(TaskBase):
    def __init__(self, mzml_path, averagine=n_glycan_averagine, charge_range=(-1, -8),
                 ms1_peak_picking_args=None, msn_peak_picking_args=None, ms1_deconvolution_args=None,
                 msn_deconvolution_args=None, start_scan_id=None, end_scan_id=None, storage_path=None,
                 sample_name=None, cache_handler_type=None):

        if cache_handler_type is None:
            cache_handler_type = DatabaseScanCacheHandler
        if isinstance(averagine, basestring):
            averagine = parse_averagine_formula(averagine)

        self.mzml_path = mzml_path
        self.storage_path = storage_path
        self.sample_name = sample_name

        self.cache_handler_type = cache_handler_type

        self.scan_generator = PipedScanGenerator(
            mzml_path, averagine=averagine, charge_range=charge_range, ms1_peak_picking_args=ms1_peak_picking_args,
            msn_peak_picking_args=msn_peak_picking_args, ms1_deconvolution_args=ms1_deconvolution_args,
            msn_deconvolution_args=msn_deconvolution_args)

        self.start_scan_id = start_scan_id
        self.end_scan_id = end_scan_id

        self.sample_run = None

    def run(self):
        self.log("Initializing Generator")
        self.scan_generator.configure_iteration(self.start_scan_id, self.end_scan_id)
        self.log("Setting Sink")
        sink = ScanSink(self.scan_generator, self.cache_handler_type)
        self.log("Initializing Cache")
        sink.configure_cache(self.storage_path, self.sample_name)

        self.log("Begin Processing")
        for scan in sink:
            self.log("Processed %s (%f)" % (scan.id, scan.scan_time))
        self.log("Finished Recieving Scans")
        sink.complete()
        self.log("Completed Sample %s" % (self.sample_name,))
        sink.commit()

        self.sample_run = sink.sample_run


class GlycanChromatogramAnalyzer(TaskBase):
    def __init__(self, database_connection, hypothesis_id, sample_run_id, adducts=None,
                 mass_error_tolerance=1e-5, grouping_error_tolerance=1.5e-5,
                 scoring_model=None, network_sharing=0.2, analysis_name=None):
        self.database_connection = database_connection
        self.hypothesis_id = hypothesis_id
        self.sample_run_id = sample_run_id
        self.mass_error_tolerance = mass_error_tolerance
        self.grouping_error_tolerance = grouping_error_tolerance
        self.scoring_model = scoring_model
        self.network_sharing = network_sharing
        self.adducts = adducts
        self.analysis_name = analysis_name
        self.analysis = None

    def save_solutions(self, solutions, peak_mapping):
        if self.analysis_name is None:
            return
        analysis_saver = AnalysisSerializer(self.database_connection, self.sample_run_id, self.analysis_name)
        analysis_saver.set_peak_lookup_table(peak_mapping)
        analysis_saver.set_analysis_type(AnalysisTypeEnum.glycan_lc_ms.name)

        analysis_saver.set_parameters({
            "hypothesis_id": self.hypothesis_id,
            "sample_run_id": self.sample_run_id,
            "mass_error_tolerance": self.mass_error_tolerance,
            "grouping_error_tolerance": self.grouping_error_tolerance,
            "network_sharing": self.network_sharing,
            "adducts": [adduct.name for adduct in self.adducts]
        })

        for chroma in solutions:
            if chroma.composition:
                analysis_saver.save_glycan_composition_chromatogram_solution(chroma)
            else:
                analysis_saver.save_unidentified_chromatogram_solution(chroma)

        self.analysis = analysis_saver.analysis
        analysis_saver.commit()

    def run(self):
        peak_loader = DatabaseScanDeserializer(
            self.database_connection, sample_run_id=self.sample_run_id)

        database = GlycanCompositionDiskBackedStructureDatabase(
            self.database_connection, self.hypothesis_id)

        extractor = ChromatogramExtractor(
            peak_loader, grouping_tolerance=self.grouping_error_tolerance)
        proc = ChromatogramProcessor(
            extractor, database, mass_error_tolerance=self.mass_error_tolerance, adducts=self.adducts,
            scoring_model=self.scoring_model, network_sharing=self.network_sharing)
        proc.start()
        self.log('Saving solutions')
        self.save_solutions(proc.solutions, extractor.peak_mapping)
        return proc


class GlycopeptideLCMSMSAnalyzer(TaskBase):
    def __init__(self, database_connection, hypothesis_id, sample_run_id,
                 analysis_name=None, grouping_error_tolerance=1.5e-5, mass_error_tolerance=1e-5,
                 msn_mass_error_tolerance=2e-5, psm_fdr_threshold=0.05, scoring_model=None):
        self.database_connection = database_connection
        self.hypothesis_id = hypothesis_id
        self.sample_run_id = sample_run_id
        self.analysis_name = analysis_name
        self.mass_error_tolerance = mass_error_tolerance
        self.msn_mass_error_tolerance = msn_mass_error_tolerance
        self.grouping_error_tolerance = grouping_error_tolerance
        self.psm_fdr_threshold = psm_fdr_threshold
        self.scoring_model = scoring_model
        self.analysis = None

    def run(self):
        peak_loader = DatabaseScanDeserializer(
            self.database_connection, sample_run_id=self.sample_run_id)

        database = GlycopeptideDiskBackedStructureDatabase(
            self.database_connection, self.hypothesis_id)

        extractor = ChromatogramExtractor(
            peak_loader, grouping_tolerance=self.grouping_error_tolerance)

        prec_info = peak_loader.precursor_information()
        msms_scans = [o.product for o in prec_info]

        # Traditional LC-MS/MS Database Search
        searcher = GlycopeptideDatabaseSearchIdentifier(
            msms_scans, BinomialSpectrumMatcher, database, peak_loader.convert_scan_id_to_retention_time)
        target_hits, decoy_hits = searcher.search(
            precursor_error_tolerance=self.mass_error_tolerance,
            error_tolerance=self.msn_mass_error_tolerance)

        searcher.target_decoy(target_hits, decoy_hits)

        # Map MS/MS solutions to chromatograms. TODO Handle MS/MS without chromatograms
        chroma_with_sols = searcher.map_to_chromatograms(tuple(extractor), target_hits, self.mass_error_tolerance)
        merged = chromatogram_mapping.aggregate_by_assigned_entity(chroma_with_sols)

        # Score chromatograms, both matched and unmatched
        self.log("Scoring chromatograms")
        chroma_scoring_model = self.scoring_model
        scored_merged = ChromatogramFilter(
            [ChromatogramSolution(c, scorer=chroma_scoring_model) for c in merged])

        self.log("Assigning consensus glycopeptides to spectrum clusters")
        gps, unassigned = identified_glycopeptide.extract_identified_structures(
            scored_merged, lambda x: x.q_value < self.psm_fdr_threshold)

        self.log("Saving solutions")
        self.save_solutions(gps, unassigned, extractor.peak_mapping)
        return gps, unassigned, target_hits, decoy_hits

    def save_solutions(self, identified_glycopeptides, unassigned_chromatograms, peak_mapping):
        if self.analysis_name is None:
            return
        analysis_saver = AnalysisSerializer(self.database_connection, self.sample_run_id, self.analysis_name)
        analysis_saver.set_peak_lookup_table(peak_mapping)
        analysis_saver.set_analysis_type(AnalysisTypeEnum.glycopeptide_lc_msms.name)
        analysis_saver.set_parameters({
            "hypothesis_id": self.hypothesis_id,
            "sample_run_id": self.sample_run_id,
            "mass_error_tolerance": self.mass_error_tolerance,
            "fragment_error_tolerance": self.msn_mass_error_tolerance,
            "grouping_error_tolerance": self.grouping_error_tolerance,
            "psm_fdr_threshold": self.psm_fdr_threshold
        })

        analysis_saver.save_glycopeptide_identification_set(identified_glycopeptides)
        for chroma in unassigned_chromatograms:
            analysis_saver.save_unidentified_chromatogram_solution(chroma)

        self.analysis = analysis_saver.analysis
        analysis_saver.commit()


class ChromatogramProfiler(TaskBase):
    def __init__(self, mzml_path, database_rules_path, averagine=n_glycan_averagine, charge_range=(-1, -8),
                 ms1_peak_picking_args=None, msn_peak_picking_args=None, ms1_deconvolution_args=None,
                 msn_deconvolution_args=None, storage_path=None, sample_name=None, analysis_name=None,
                 cache_handler_type=None, aggregate=True):
        if cache_handler_type is None:
            cache_handler_type = DatabaseScanCacheHandler
        if isinstance(averagine, basestring):
            averagine = parse_averagine_formula(averagine)

        self.mzml_path = mzml_path
        self.storage_path = storage_path
        self.sample_name = sample_name
        self.analysis_name = analysis_name

        self.cache_handler_type = cache_handler_type

        self.scan_generator = PipedScanGenerator(
            mzml_path, averagine=averagine, charge_range=charge_range, ms1_peak_picking_args=ms1_peak_picking_args,
            msn_peak_picking_args=msn_peak_picking_args, ms1_deconvolution_args=ms1_deconvolution_args,
            msn_deconvolution_args=msn_deconvolution_args)

        self.database_rules_path = database_rules_path
        if isinstance(database_rules_path, basestring):
            self.database = build_database(database_rules_path)
        else:
            self.database = database_rules_path

        self.tracer = None
        self.adducts = []

        self.chromatograms = None
        self.solutions = None
        self._aggregate = aggregate

    def search(self, mass_error_tolerance=1e-5, grouping_mass_error_tolerance=None, start_scan=None,
               max_scans=None, end_scan=None, adducts=None, truncate=False, minimum_mass=500):

        start = time.time()

        if adducts is None:
            adducts = []
        self.adducts = adducts

        logger.info("Begin Chromatogram Tracing")
        self.scan_generator.configure_iteration(
            start_scan=start_scan, end_scan=end_scan, max_scans=max_scans)
        if self._aggregate:
            self.tracer = IncludeUnmatchedTracer(
                self.scan_generator, self.database, mass_error_tolerance,
                cache_handler_type=self.cache_handler_type)
        else:
            self.tracer = NonAggregatingTracer(
                self.scan_generator, cache_handler_type=self.cache_handler_type)
        self.tracer.configure_cache(self.storage_path, self.sample_name)

        i = 0
        for case in self.tracer:
            self.log("%d, %d, %s, %s, %d" % (
                i, case[1].index, case[1].scan_time, case[1].id, len(case[0])))
            i += 1
            if end_scan == case[1].id or i == max_scans:
                break

        self.tracer.commit()
        self.tracer.complete()

        if grouping_mass_error_tolerance is None:
            grouping_mass_error_tolerance = mass_error_tolerance * 1.5

        self.build_chromatograms(
            mass_error_tolerance, grouping_mass_error_tolerance, adducts, truncate)
        self.log("Tracing Complete (%r minutes elapsed)" % ((time.time() - start) / 60.,))

    def build_chromatograms(self, mass_error_tolerance, grouping_mass_error_tolerance, adducts,
                            truncate=False, minimum_mass=500):
        self.log("Post-Processing Chromatogram Traces (%0.3e, %0.3e, %r, %r)" % (
            mass_error_tolerance, grouping_mass_error_tolerance, adducts, truncate))
        self.chromatograms = self.tracer.build_chromatograms(
            grouping_tolerance=grouping_mass_error_tolerance,
            truncate=truncate,
            minimum_mass=minimum_mass)
        self._convert_to_entity_chromatograms()
        self.chromatograms = reverse_adduction_search(
            join_mass_shifted(
                ChromatogramFilter(self.chromatograms), adducts, mass_error_tolerance),
            adducts, mass_error_tolerance, self.database)
        self._convert_to_entity_chromatograms()

    def _convert_to_entity_chromatograms(self, entity_chromatogram_type=GlycanCompositionChromatogram):
        acc = []
        for chroma in self.chromatograms:
            copied = chroma.clone(entity_chromatogram_type)
            copied.entity = copied.composition
            acc.append(copied)
        self.chromatograms = ChromatogramFilter(acc, sort=False)

    def _convert_solution_to_entity_chromatogram(self, entity_chromatogram_type=GlycanCompositionChromatogram):
        for chroma in self.solutions:
            copied = chroma.chromatogram.clone(entity_chromatogram_type)
            copied.entity = copied.composition
            chroma.chromatogram = copied

    def _evaluate_fits(self, chromatograms, base_coef=0.8, support_coef=0.2, rt_delta=0.25,
                       scoring_model=None, min_points=3, smooth=True):
        solutions = []
        if scoring_model is None:
            scoring_model = ChromatogramScorer()

        filtered = ChromatogramFilter.process(
            chromatograms, delta_rt=rt_delta, min_points=min_points)

        if smooth:
            filtered = ChromatogramOverlapSmoother(filtered)

        for case in filtered:
            try:
                solutions.append(ChromatogramSolution(case, scorer=scoring_model))
            except (IndexError, ValueError) as e:
                print case, e, len(case)
                continue

        if base_coef != 1.0:
            NetworkScoreDistributor(solutions, self.database.network).distribute(base_coef, support_coef)
        return solutions

    def score(self, base_coef=0.8, support_coef=0.2, rt_delta=0.25, scoring_model=None, min_points=3,
              smooth=True):
        if scoring_model is None:
            scoring_model = ChromatogramScorer()

        self.solutions = self._evaluate_fits(
            self.chromatograms, base_coef, support_coef, rt_delta, scoring_model, min_points,
            smooth)

        if self.adducts:
            hold = prune_bad_adduct_branches(ChromatogramFilter(self.solutions))
            self.solutions = self._evaluate_fits(hold, base_coef, support_coef, rt_delta, scoring_model)

        self.solutions = ChromatogramFilter(sol for sol in self.solutions if sol.score > 1e-5)
        self._convert_solution_to_entity_chromatogram()
        return self._filter_accepted()

    def _filter_accepted(self, threshold=0.4):
        self.accepted_solutions = ChromatogramFilter([
            sol for sol in self.solutions
            if sol.score > threshold and not sol.used_as_adduct
        ])
        return self.accepted_solutions

    def plot(self, min_score=0.4, min_signal=0.2, colorizer=None, chromatogram_artist=None, include_tic=True):
        if chromatogram_artist is None:
            chromatogram_artist = plotting.SmoothingChromatogramArtist
        monosaccharides = set()

        for sol in self.solutions:
            if sol.glycan_composition:
                monosaccharides.update(map(str, sol.glycan_composition))

        label_abundant = plotting.AbundantLabeler(
            plotting.NGlycanLabelProducer(monosaccharides),
            max(sol.total_signal for sol in self.solutions if sol.score > min_score) * min_signal)

        if colorizer is None:
            colorizer = plotting.n_glycan_colorizer

        results = [sol for sol in self.solutions if sol.score > min_score and not sol.used_as_adduct]
        chrom = chromatogram_artist(results, colorizer=colorizer).draw(label_function=label_abundant)
        if include_tic:
            chrom.draw_generic_chromatogram(
                "TIC",
                map(self.tracer.scan_id_to_rt, self.tracer.total_ion_chromatogram),
                self.tracer.total_ion_chromatogram.values(), 'blue')
            chrom.ax.set_ylim(0, max(self.tracer.total_ion_chromatogram.values()) * 1.1)

        agg = plotting.AggregatedAbundanceArtist(results)
        agg.draw()
        return chrom, agg


ChromatogramProfiler.log_with_logger(logger)


class ScanDatabaseChromatogramProfiler(ChromatogramProfiler):
    def __init__(self, scan_db_path, composition_database_rules_path, storage_path=None,
                 sample_name=None, analysis_name=None, cache_handler_type=None):
        if cache_handler_type is None:
            cache_handler_type = NullScanCacheHandler

        self.scan_generator = DatabaseScanGenerator(scan_db_path, sample_name)

        self.storage_path = storage_path
        self.sample_name = sample_name
        self.analysis_name = analysis_name

        self.database_rules_path = composition_database_rules_path
        if isinstance(composition_database_rules_path, basestring):
            self.database = build_database(composition_database_rules_path)
        else:
            self.database = composition_database_rules_path

        self.cache_handler_type = cache_handler_type

        self.tracer = None
        self.adducts = []

        self.chromatograms = None
        self.solutions = None

        self._aggregate = True


GlycanProfiler = ChromatogramProfiler
ScanDatabaseGlycanProfiler = ScanDatabaseChromatogramProfiler
