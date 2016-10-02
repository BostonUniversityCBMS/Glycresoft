from ms_deisotope.peak_dependency_network.intervals import Interval, IntervalTreeNode

from glycan_profiling.task import TaskBase

from .chromatogram import (
    Chromatogram)


class ChromatogramForest(object):
    def __init__(self, chromatograms=None, error_tolerance=1e-5, scan_id_to_rt=lambda x: x):
        if chromatograms is None:
            chromatograms = []
        self.chromatograms = sorted(chromatograms, key=lambda x: x.neutral_mass)
        self.error_tolerance = error_tolerance
        self.scan_id_to_rt = scan_id_to_rt
        self.count = 0
        self.verbose = False

    def __len__(self):
        return len(self.chromatograms)

    def __iter__(self):
        return iter(self.chromatograms)

    def __getitem__(self, i):
        if isinstance(i, (int, slice)):
            return self.chromatograms[i]
        else:
            return [self.chromatograms[j] for j in i]

    def find_insertion_point(self, peak):
        index, matched = binary_search_with_flag(
            self.chromatograms, peak.neutral_mass, self.error_tolerance)
        return index, matched

    def find_minimizing_index(self, peak, indices):
        best_index = None
        best_error = float('inf')
        for index_case in indices:
            chroma = self[index_case]
            err = abs(chroma.neutral_mass - peak.neutral_mass) / peak.neutral_mass
            if err < best_error:
                best_index = index_case
                best_error = err
        return best_index

    def handle_peak(self, scan_id, peak):
        if len(self) == 0:
            index = [0]
            matched = False
        else:
            index, matched = self.find_insertion_point(peak)
        if matched:
            chroma = self.chromatograms[self.find_minimizing_index(peak, index)]
            most_abundant_member = chroma.most_abundant_member
            chroma.insert(scan_id, peak, self.scan_id_to_rt(scan_id))
            if peak.intensity < most_abundant_member:
                chroma.retain_most_abundant_member()
        else:
            chroma = Chromatogram(None)
            chroma.created_at = "forest"
            chroma.insert(scan_id, peak, self.scan_id_to_rt(scan_id))
            self.insert_chromatogram(chroma, index)
        self.count += 1

    def insert_chromatogram(self, chromatogram, index):
        if index[0] != 0:
            self.chromatograms.insert(index[0] + 1, chromatogram)
        else:
            if len(self) == 0:
                new_index = index[0]
            else:
                x = self.chromatograms[index[0]]
                if x.neutral_mass < chromatogram.neutral_mass:
                    new_index = index[0] + 1
                else:
                    new_index = index[0]
            self.chromatograms.insert(new_index, chromatogram)

    def aggregate_unmatched_peaks(self, scan_id_peaks_list, minimum_mass=300, minimum_intensity=1000.):
        unmatched = sorted(scan_id_peaks_list, key=lambda x: x[1].intensity, reverse=True)
        for scan_id, peak in unmatched:
            if peak.neutral_mass < minimum_mass or peak.intensity < minimum_intensity:
                continue
            self.handle_peak(scan_id, peak)

            self.verbose = False


class ChromatogramMerger(object):
    def __init__(self, chromatograms=None, error_tolerance=1e-5):
        if chromatograms is None:
            chromatograms = []
        self.chromatograms = sorted(chromatograms, key=lambda x: x.neutral_mass)
        self.error_tolerance = error_tolerance
        self.count = 0
        self.verbose = False

    def __len__(self):
        return len(self.chromatograms)

    def __iter__(self):
        return iter(self.chromatograms)

    def __getitem__(self, i):
        if isinstance(i, (int, slice)):
            return self.chromatograms[i]
        else:
            return [self.chromatograms[j] for j in i]

    def find_insertion_point(self, new_chromatogram):
        index, matched = binary_search_with_flag(
            self.chromatograms, new_chromatogram.neutral_mass, self.error_tolerance)
        return index, matched

    def find_minimizing_index(self, new_chromatogram, indices):
        best_index = None
        best_error = float('inf')
        for index_case in indices:
            chroma = self[index_case]
            err = abs(chroma.neutral_mass - new_chromatogram.neutral_mass) / new_chromatogram.neutral_mass
            if err < best_error:
                best_index = index_case
                best_error = err
        return best_index

    def merge_overlaps(self, new_chromatogram, chromatogram_range):
        has_merged = False
        for chroma in chromatogram_range:
            if chroma.overlaps_in_time(chroma) and abs(
                    (chroma.neutral_mass - new_chromatogram.neutral_mass) / new_chromatogram.neutral_mass):
                chroma.merge(new_chromatogram)
                has_merged = True
                break
        return has_merged

    def handle_new_chromatogram(self, new_chromatogram):
        if len(self) == 0:
            index = [0]
            matched = False
        else:
            index, matched = self.find_insertion_point(new_chromatogram)
        if matched:

            chroma = self[index]
            has_merged = self.merge_overlaps(new_chromatogram, chroma)
            if not has_merged:
                minimized_index = self.find_minimizing_index(new_chromatogram, index)
                self.insert_chromatogram(new_chromatogram, [minimized_index])
        else:
            self.insert_chromatogram(new_chromatogram, index)
        self.count += 1

    def insert_chromatogram(self, chromatogram, index):
        if index[0] != 0:
            self.chromatograms.insert(index[0] + 1, chromatogram)
        else:
            if len(self) == 0:
                new_index = index[0]
            else:
                x = self.chromatograms[index[0]]
                if x.neutral_mass < chromatogram.neutral_mass:
                    new_index = index[0] + 1
                else:
                    new_index = index[0]
            self.chromatograms.insert(new_index, chromatogram)

    def aggregate_chromatograms(self, chromatograms):
        unmatched = sorted(chromatograms, key=lambda x: x.total_signal, reverse=True)
        for chroma in unmatched:
            self.handle_new_chromatogram(chroma)


def flatten_tree(tree):
    output_queue = []
    input_queue = [tree]
    while input_queue:
        next_node = input_queue.pop()
        output_queue.append(next_node)

        next_right = next_node.right
        if next_right is not None:
            input_queue.append(next_right)

        next_left = next_node.left
        if next_left is not None:
            input_queue.append(next_left)
    return output_queue[::-1]


def layered_traversal(nodes):
    return sorted(nodes, key=lambda x: (x.level, x.center), reverse=True)


class ChromatogramOverlapSmoother(object):
    def __init__(self, chromatograms, error_tolerance=1e-5):
        self.retention_interval_tree = build_rt_interval_tree(chromatograms)
        self.error_tolerance = error_tolerance
        self.solution_map = {None: []}
        self.chromatograms = self.smooth()

    def __iter__(self):
        return iter(self.chromatograms)

    def __getitem__(self, i):
        return self.chromatograms[i]

    def __len__(self):
        return len(self.chromatograms)

    def aggregate_interval(self, tree):
        chromatograms = [interval[0] for interval in tree.contained]
        chromatograms.extend(self.solution_map[tree.left])
        chromatograms.extend(self.solution_map[tree.right])
        merger = ChromatogramMerger(error_tolerance=self.error_tolerance)
        merger.aggregate_chromatograms(chromatograms)
        self.solution_map[tree] = list(merger)
        return merger

    def smooth(self):
        nodes = layered_traversal(flatten_tree(self.retention_interval_tree))
        for node in nodes:
            self.aggregate_interval(node)
        return self.solution_map[self.retention_interval_tree]


def mask_subsequence(target, masker):
    unmasked_nodes = []
    target_nodes = target.nodes.unspool_strip_children()
    masking_nodes = masker.nodes.unspool()
    for node in target_nodes:
        if node not in masking_nodes:
            unmasked_nodes.append(node)
    new = Chromatogram(target.composition)
    new.created_at = "mask_subsequence"
    map(new.insert_node, unmasked_nodes)
    return new


def binary_search_with_flag(array, mass, error_tolerance=1e-5):
        lo = 0
        n = hi = len(array)
        while hi != lo:
            mid = (hi + lo) / 2
            x = array[mid]
            err = (x.neutral_mass - mass) / mass
            if abs(err) <= error_tolerance:
                i = mid - 1
                # Begin Sweep forward
                while i > 0:
                    x = array[i]
                    err = (x.neutral_mass - mass) / mass
                    if abs(err) <= error_tolerance:
                        i -= 1
                        continue
                    else:
                        break
                low_end = i
                i = mid + 1

                # Begin Sweep backward
                while i < n:
                    x = array[i]
                    err = (x.neutral_mass - mass) / mass
                    if abs(err) <= error_tolerance:
                        i += 1
                        continue
                    else:
                        break
                high_end = i
                return list(range(low_end, high_end)), True
            elif (hi - lo) == 1:
                return [mid], False
            elif err > 0:
                hi = mid
            elif err < 0:
                lo = mid
        return 0, False


def is_sorted(mass_list):
    i = 0
    for a, b in zip(mass_list[:-1], mass_list[1:]):
        if not a.neutral_mass <= b.neutral_mass:
            print a.neutral_mass, b.neutral_mass, i
            raise ValueError("Not sorted")
            return False
        i += 1
    return True


def is_sparse(mass_list):
    i = 0
    for a, b in zip(mass_list[:-1], mass_list[1:]):
        err = (a.neutral_mass - b.neutral_mass) / b.neutral_mass
        if abs(err) < 1e-5 and a.composition is None:
            print a.neutral_mass, b.neutral_mass, err, i
            raise ValueError("Not sparse")
            return False
        i += 1
    return True


def is_sparse_and_disjoint(chromatogram_list):
    i = 0
    n = len(chromatogram_list)
    for i in range(n - 1):
        a = chromatogram_list[i]
        b = chromatogram_list[i + 1]
        err = (a.neutral_mass - b.neutral_mass) / b.neutral_mass
        if abs(err) < 1e-5 and a.composition is None and a.overlaps_in_time(b):
            print a.neutral_mass, b.neutral_mass, err, i
            raise ValueError("Not sparse")
            return False
    return True


def distill_peaks(chromatograms):
    peaks = set()
    for chroma in chromatograms:
        for node in chroma.nodes.unspool():
            for peak in node.members:
                peaks.add((node.scan_id, peak))
    return peaks


def smooth_overlaps(chromatogram_list, error_tolerance=1e-5):
    chromatogram_list = sorted(chromatogram_list, key=lambda x: x.neutral_mass)
    out = []
    last = chromatogram_list[0]
    i = 1
    while i < len(chromatogram_list):
        current = chromatogram_list[i]
        mass_error = abs((last.neutral_mass - current.neutral_mass) / current.neutral_mass)
        if mass_error <= error_tolerance:
            if last.overlaps_in_time(current):
                last = last.merge(current)
                last.created_at = "smooth_overlaps"
            else:
                out.append(last)
                last = current
        else:
            out.append(last)
            last = current
        i += 1
    out.append(last)
    return out


class ChromatogramRetentionTimeInterval(Interval):
    def __init__(self, chromatogram):
        super(ChromatogramRetentionTimeInterval, self).__init__(
            chromatogram.start_time, chromatogram.end_time, [chromatogram])
        self.neutral_mass = chromatogram.neutral_mass
        self.start_time = self.start
        self.end_time = self.end
        self.data['neutral_mass'] = self.neutral_mass


def build_rt_interval_tree(chromatogram_list, interval_tree_type=IntervalTreeNode):
    intervals = list(map(ChromatogramRetentionTimeInterval, chromatogram_list))
    interval_tree = interval_tree_type.build(intervals)
    return interval_tree
