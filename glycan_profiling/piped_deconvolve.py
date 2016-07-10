import multiprocessing

import ms_peak_picker
import ms_deisotope

from ms_deisotope.processor import MzMLLoader

from multiprocessing import Process, Queue
try:
    from Queue import Empty as QueueEmpty
except:
    from queue import Empty as QueueEmpty


DONE = b"--NO-MORE--"


def pick_peaks(scan):
    scan.pick_peaks(transforms=[
        ms_peak_picker.scan_filter.FTICRBaselineRemoval(scale=2.),
        ms_peak_picker.scan_filter.SavitskyGolayFilter()], start_mz=200)
    return scan


class ScanIDYieldingProcess(Process):
    def __init__(self, mzml_path, queue, start_scan=None, max_scans=None, end_scan=None, no_more_event=None):
        Process.__init__(self)
        self.mzml_path = mzml_path
        self.queue = queue
        self.loader = None

        self.start_scan = start_scan
        self.max_scans = max_scans
        self.end_scan = end_scan
        self.no_more_event = no_more_event

    def run(self):
        self.loader = MzMLLoader(self.mzml_path)

        scan_id_index = list(self.loader._source._offset_index)

        index = 0
        if self.start_scan is not None:
            index = scan_id_index.index(self.start_scan)

        n = len(scan_id_index)

        count = 0
        if self.max_scans is None:
            max_scans = float('inf')
        else:
            max_scans = self.max_scans

        end_scan = self.end_scan

        while index < n and count < max_scans:
            scan_id = scan_id_index[index]
            if scan_id == end_scan:
                break
            self.queue.put(scan_id)
            index += 1
            count += 1

        if self.no_more_event is not None:
            self.no_more_event.set()
        else:
            self.queue.put(DONE)


class ScanTransformingProcess(Process):
    def __init__(self, mzml_path, input_queue, output_queue,
                 averagine=ms_deisotope.averagine.glycan, no_more_event=None):
        Process.__init__(self)
        self.mzml_path = mzml_path
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.averagine = dict(averagine)
        self.no_more_event = no_more_event

    def run(self):
        loader = MzMLLoader(self.mzml_path)

        has_input = True
        averagine_cache = ms_deisotope.averagine.AveragineCache(self.averagine)
        while has_input:
            try:
                scan_id = self.input_queue.get(True, 20)
            except QueueEmpty:
                if self.no_more_event is not None and self.no_more_event.is_set():
                    has_input = False
                continue

            if scan_id == DONE:
                has_input = False
                break
            try:
                scan = loader.get_scan_by_id(scan_id)
            except Exception:
                import traceback
                traceback.print_exc()
                continue
            if len(scan.arrays[0]) == 0:
                continue
            pick_peaks(scan)
            deconvolve(scan, averagine_cache)

            self.output_queue.put(scan.pack())

        if self.no_more_event is None:
            self.output_queue.put(DONE)


class PeakPickingProcess(Process):
    def __init__(self, mzml_path, queue, start_scan=None, max_scans=None, end_scan=None, no_more_event=None):
        Process.__init__(self)
        self.mzml_path = mzml_path
        self.queue = queue
        self.loader = None

        self.start_scan = start_scan
        self.max_scans = max_scans
        self.end_scan = end_scan
        self.no_more_event = no_more_event

    def run(self):
        self.loader = MzMLLoader(self.mzml_path)

        if self.start_scan:
            self.loader.start_from_scan(self.start_scan)

        i = 0
        for scan, _ in self.loader:
            _ = scan.id, scan.scan_time, scan.index
            if len(scan.arrays[0]):
                self.queue.put(pick_peaks(scan))

            if scan.id == self.end_scan or i == self.max_scans:
                break
            i += 1

        if self.no_more_event is not None:
            self.no_more_event.set()
            # print "No More Event Set"
        else:
            self.queue.put(DONE)


def deconvolve(scan, averagine=ms_deisotope.averagine.glycan):
    dp, _ = ms_deisotope.deconvolution.deconvolute_peaks(scan.peak_set, charge_range=(-1, -8), **{
        "averagine": averagine,
        "scorer": ms_deisotope.scoring.PenalizedMSDeconVFitter(15)
    })
    scan.deconvoluted_peak_set = dp
    return scan


class DeconvolutingProcess(Process):
    def __init__(self, input_queue, output_queue, averagine=ms_deisotope.averagine.glycan, no_more_event=None):
        Process.__init__(self)
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.averagine = dict(averagine)
        self.no_more_event = no_more_event

    def run(self):
        has_input = True
        averagine_cache = ms_deisotope.averagine.AveragineCache(self.averagine)
        while has_input:
            try:
                scan = self.input_queue.get(True, 20)
            except QueueEmpty:
                if self.no_more_event is not None and self.no_more_event.is_set():
                    has_input = False
                continue

            if scan == DONE:
                has_input = False
                break
            deconvolve(scan, averagine_cache)
            self.output_queue.put(scan.pack())

        if self.no_more_event is None:
            self.output_queue.put(DONE)


class ScanOrderManager(object):
    def __init__(self, queue, done_event, helper_producers=None):
        if helper_producers is None:
            helper_producers = []
        self.queue = queue
        self.last_index = None
        self.count_jobs_done = 0
        self.waiting = {}
        self.done_event = done_event
        self.helper_producers = helper_producers
        self.started_helpers = False

    def consume(self, timeout=10):
        try:
            item = self.queue.get(True, timeout)
            if item == DONE:
                item = self.queue.get(True, timeout)
            self.waiting[item.index] = item
            return True
        except QueueEmpty:
            return False

    def start_helper_producers(self):
        if self.started_helpers:
            return
        self.started_helpers = True
        for helper in self.helper_producers:
            if helper.is_alive():
                continue
            helper.start()

    def __iter__(self):
        has_more = True
        while has_more:
            if self.consume():
                self.count_jobs_done += 1
            if self.last_index is None:
                # print "First Check"
                keys = sorted(self.waiting)
                if keys:
                    scan = self.waiting.pop(keys[0])
                    self.last_index = scan.index
                    yield scan
                self.start_helper_producers()
            elif self.last_index + 1 in self.waiting:
                scan = self.waiting.pop(self.last_index + 1)
                self.last_index = scan.index
                yield scan
            elif len(self.waiting) == 0:
                # print "No More Waiting"
                if self.done_event.is_set():
                    # print "Last Check"
                    has_something = self.consume()
                    # print "Had Something?", has_something
                    # print self.waiting
                    if not has_something:
                        # print "Quitting"
                        has_more = False
                        # raise StopIteration()


class ScanGenerator(object):
    number_of_helper_deconvoluters = 4

    def __init__(self, mzml_file, averagine=ms_deisotope.averagine.glycan, number_of_helper_deconvoluters=4):
        self.mzml_file = mzml_file
        self.averagine = averagine
        self.time_cache = {}

        self._iterator = None

        self._picker_process = None
        self._deconv_process = None

        self._input_queue = None
        self._output_queue = None
        self._deconv_helpers = None
        self._order_manager = None

        self.number_of_helper_deconvoluters = number_of_helper_deconvoluters

    def join(self):
        if self._picker_process is not None:
            self._picker_process.join()
        if self._deconv_process is not None:
            self._deconv_process.join()
        if self._deconv_helpers is not None:
            for helper in self._deconv_helpers:
                helper.join()

    def _terminate(self):
        if self._picker_process is not None:
            self._picker_process.terminate()
        if self._deconv_process is not None:
            self._deconv_process.terminate()
        if self._deconv_helpers is not None:
            for helper in self._deconv_helpers:
                helper.terminate()

    def make_iterator(self, start_scan=None, end_scan=None, max_scans=None):
        self._input_queue = Queue(100)
        self._output_queue = Queue(100)

        self._terminate()

        done_event = multiprocessing.Event()

        # self._picker_process = PeakPickingProcess(
        #     self.mzml_file, self._input_queue, start_scan=start_scan, end_scan=end_scan,
        #     max_scans=max_scans, no_more_event=done_event)
        self._picker_process = ScanIDYieldingProcess(
            self.mzml_file, self._input_queue, start_scan=start_scan, end_scan=end_scan,
            max_scans=max_scans, no_more_event=done_event)
        self._picker_process.start()

        # self._deconv_process = DeconvolutingProcess(
        #     self._input_queue, self._output_queue, self.averagine, done_event)
        self._deconv_process = ScanTransformingProcess(
            self.mzml_file,
            self._input_queue, self._output_queue, self.averagine, done_event)
        self._deconv_helpers = []

        for i in range(self.number_of_helper_deconvoluters):
            self._deconv_helpers.append(
                # DeconvolutingProcess(
                #     self._input_queue, self._output_queue, self.averagine, done_event))
                ScanTransformingProcess(
                    self.mzml_file,
                    self._input_queue, self._output_queue, self.averagine, done_event))
        self._deconv_process.start()

        self._order_manager = ScanOrderManager(
            self._output_queue, done_event, self._deconv_helpers)

        for scan in self._order_manager:
            self.time_cache[scan.id] = scan.scan_time
            yield scan

    def configure_iteration(self, start_scan=None, end_scan=None, max_scans=None):
        self._iterator = self.make_iterator(start_scan, end_scan, max_scans)

    def __iter__(self):
        return self

    def __next__(self):
        if self._iterator is None:
            self._iterator = self.make_iterator()
        return next(self._iterator)

    def convert_scan_id_to_retention_time(self, scan_id):
        return self.time_cache[scan_id]

    next = __next__

if __name__ == '__main__':
    import sys
    import time
    mzml_file = sys.argv[1]
    start_scan = sys.argv[2]
    max_scans = 50

    gen = ScanGenerator(mzml_file)
    gen.configure_iteration(start_scan=start_scan, max_scans=max_scans)

    has_output = True
    last = time.time()
    start_time = last
    i = 0
    for scan in gen:
        now = time.time()
        print i, scan.deconvoluted_peak_set, scan.id, scan.index, now - last
        last = now
        i += 1
    print "Finished", last - start_time
