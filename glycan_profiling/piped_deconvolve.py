from collections import deque
import multiprocessing

import dill
import ms_peak_picker
import ms_deisotope

import traceback

from ms_deisotope.processor import MzMLLoader, ScanProcessor

from multiprocessing import Process, Queue
try:
    from Queue import Empty as QueueEmpty
except:
    from queue import Empty as QueueEmpty


DONE = b"--NO-MORE--"
SCAN_STATUS_GOOD = b"good"
SCAN_STATUS_SKIP = b"skip"

resampler = ms_peak_picker.scan_filter.LinearResampling(0.0001)
savgol = ms_peak_picker.scan_filter.SavitskyGolayFilter()
denoise = ms_peak_picker.scan_filter.FTICRBaselineRemoval(scale=2.)


def pick_peaks(scan, remove_baseline=True, smooth=True, start_mz=200.):
    transforms = []
    if remove_baseline:
        transforms.append(denoise)
    if smooth:
        # transforms.append(resampler)
        transforms.append(savgol)
    scan.pick_peaks(transforms=transforms, start_mz=start_mz)
    return scan


def deconvolve(scan, averagine=ms_deisotope.averagine.glycan, charge_range=(-1, -8), scorer=None):
    if scorer is None:
        scorer = ms_deisotope.scoring.PenalizedMSDeconVFitter(15, 2.)
    dp, _ = ms_deisotope.deconvolution.deconvolute_peaks(
        scan.peak_set, charge_range=charge_range,
        averagine=averagine,
        scorer=scorer)
    scan.deconvoluted_peak_set = dp
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

        index = 0
        if self.start_scan is not None:
            self.loader.start_from_scan(self.start_scan)

        count = 0
        if self.max_scans is None:
            max_scans = float('inf')
        else:
            max_scans = self.max_scans

        end_scan = self.end_scan

        while count < max_scans:
            scan, products = next(self.loader)
            scan_id = scan.id
            if scan_id == end_scan:
                break
            self.queue.put((scan_id, [p.id for p in products]))
            index += 1
            count += 1

        if self.no_more_event is not None:
            self.no_more_event.set()
        else:
            self.queue.put(DONE)


class ScanBunchLoader(object):
    def __init__(self, mzml_loader):
        self.loader = mzml_loader
        self.queue = deque()

    def put(self, scan_id, product_scan_ids):
        self.queue.append((scan_id, product_scan_ids))

    def get(self):
        scan_id, product_scan_ids = self.queue.popleft()
        precursor = self.loader.get_scan_by_id(scan_id)
        products = [self.loader.get_scan_by_id(pid) for pid in product_scan_ids]
        precursor.product_scans = products
        return (precursor, products)

    def next(self):
        return self.get()

    def __next__(self):
        return self.get()


class ScanTransformingProcess(Process):
    def __init__(self, mzml_path, input_queue, output_queue,
                 averagine=ms_deisotope.averagine.glycan, charge_range=(-1, -8),
                 no_more_event=None, ms1_peak_picking_args=None, msn_peak_picking_args=None,
                 ms1_deconvolution_args=None, msn_deconvolution_args=None):

        if ms1_peak_picking_args is None:
            ms1_peak_picking_args = {
                "transforms": [denoise, savgol],
                "start_mz": 250
            }
        if msn_peak_picking_args is None:
            msn_peak_picking_args = {
                "transforms": []
            }
        if ms1_deconvolution_args is None:
            ms1_deconvolution_args = {
                "scorer": ms_deisotope.scoring.PenalizedMSDeconVFitter(15, 2.),
                "charge_range": charge_range,
                "averagine": averagine
            }
        if msn_deconvolution_args is None:
            msn_deconvolution_args = {
                "scorer": ms_deisotope.scoring.MSDeconVFitter(2.),
                "charge_range": charge_range,
                "averagine": averagine
            }

        Process.__init__(self)
        self.mzml_path = mzml_path
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.averagine = dict(averagine)
        self.charge_range = charge_range
        self.no_more_event = no_more_event
        self.ms1_peak_picking_args = ms1_peak_picking_args
        self.msn_peak_picking_args = msn_peak_picking_args
        self.ms1_deconvolution_args = ms1_deconvolution_args
        self.ms1_deconvolution_args.setdefault("charge_range", self.charge_range)
        self.ms1_deconvolution_args.setdefault("averagine", averagine)
        self.msn_deconvolution_args = msn_deconvolution_args
        self.msn_deconvolution_args.setdefault("charge_range", self.charge_range)
        self.msn_deconvolution_args.setdefault("averagine", averagine)

        self._work_complete = multiprocessing.Event()

    def log_error(self, error, scan_id, scan, product_scan_ids):
        print(error, "@", scan_id, scan.index, len(product_scan_ids), multiprocessing.current_process())
        traceback.print_exc()

    def log_message(self, message, *args):
        print(message, args, multiprocessing.current_process())

    def skip_scan(self, scan):
        self.output_queue.put((SCAN_STATUS_SKIP, scan.index, scan.ms_level))

    def send_scan(self, scan):
        self.output_queue.put((scan.pack(), scan.index, scan.ms_level))

    def all_work_done(self):
        return self._work_complete.is_set()

    def run(self):
        loader = MzMLLoader(self.mzml_path)
        queued_loader = ScanBunchLoader(loader)

        has_input = True
        transformer = ScanProcessor(
            None,
            ms1_peak_picking_args=self.ms1_peak_picking_args,
            msn_peak_picking_args=self.msn_peak_picking_args,
            ms1_deconvolution_args=self.ms1_deconvolution_args,
            msn_deconvolution_args=self.msn_deconvolution_args,
            loader_type=lambda x: x)

        while has_input:
            try:
                scan_id, product_scan_ids = self.input_queue.get(True, 20)
            except QueueEmpty:
                if self.no_more_event is not None and self.no_more_event.is_set():
                    has_input = False
                continue

            if scan_id == DONE:
                has_input = False
                break

            queued_loader.put(scan_id, product_scan_ids)
            scan, product_scans = queued_loader.get()

            if len(scan.arrays[0]) == 0:
                self.skip_scan(scan)
                continue

            try:
                scan, priorities, product_scans = transformer.process_scan_group(scan, product_scans)
                transformer.deconvolute_precursor_scan(scan, priorities)
                self.send_scan(scan)
            except Exception, e:
                self.skip_scan(scan)
                self.log_error(e, scan_id, scan, (product_scan_ids))

            for product_scan in product_scans:
                if len(product_scan.arrays[0]) == 0:
                    self.skip_scan(product_scan)
                    continue
                try:
                    transformer.pick_product_scan_peaks(product_scan)
                    transformer.deconvolute_product_scan(product_scan)
                    self.send_scan(product_scan)
                except Exception, e:
                    self.skip_scan(product_scan)
                    self.log_error(e, product_scan.id, product_scan, (product_scan_ids))

        self.log_message("Done")

        if self.no_more_event is None:
            self.output_queue.put((DONE, DONE, DONE))

        self._work_complete.set()


class ScanOrderManager(object):
    def __init__(self, queue, done_event, helper_producers=None, primary_worker=None):
        if helper_producers is None:
            helper_producers = []
        self.queue = queue
        self.last_index = None
        self.count_jobs_done = 0
        self.count_since_last = 0
        self.waiting = {}
        self.done_event = done_event
        self.helper_producers = helper_producers
        self.started_helpers = False
        self.primary_worker = primary_worker

    def all_workers_done(self):
        if self.done_event.is_set():
            if self.primary_worker.all_work_done():
                for helper in self.helper_producers:
                    if not helper.all_work_done():
                        return False
                return True
            else:
                return False
        return False

    def consume(self, timeout=10):
        try:
            item, index, status = self.queue.get(True, timeout)
            if item == DONE:
                item, index, status = self.queue.get(True, timeout)
            self.waiting[index] = item
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

    def produce(self, scan):
        self.count_since_last = 0
        return scan

    def print_state(self):
        print(self.count_since_last)
        print sorted(self.waiting.keys())
        print self.last_index
        print self.queue.qsize()

    def __iter__(self):
        has_more = True
        while has_more:
            if self.consume(1):
                self.count_jobs_done += 1
            if self.last_index is None:
                keys = sorted(self.waiting)
                if keys:
                    scan = self.waiting.pop(keys[0])
                    if scan == SCAN_STATUS_SKIP:
                        continue
                    self.last_index = scan.index
                    yield self.produce(scan)
                    self.start_helper_producers()
            elif self.last_index + 1 in self.waiting:
                scan = self.waiting.pop(self.last_index + 1)
                if scan == SCAN_STATUS_SKIP:
                    self.last_index += 1
                    continue
                self.last_index = scan.index
                yield self.produce(scan)
            elif len(self.waiting) == 0:
                if self.all_workers_done():
                    has_something = self.consume()
                    if not has_something:
                        has_more = False
            else:
                self.count_since_last += 1
                if self.count_since_last % 10 == 0:
                    self.print_state()


class ScanGenerator(object):
    number_of_helper_deconvoluters = 4

    def __init__(self, mzml_file, averagine=ms_deisotope.averagine.glycan, charge_range=(-1, -8),
                 number_of_helper_deconvoluters=4, ms1_peak_picking_args=None, msn_peak_picking_args=None,
                 ms1_deconvolution_args=None, msn_deconvolution_args=None):
        self.mzml_file = mzml_file
        self.averagine = averagine
        self.time_cache = {}
        self.charge_range = charge_range

        self._iterator = None

        self._picker_process = None
        self._deconv_process = None

        self._input_queue = None
        self._output_queue = None
        self._deconv_helpers = None
        self._order_manager = None

        self.number_of_helper_deconvoluters = number_of_helper_deconvoluters

        self.ms1_peak_picking_args = ms1_peak_picking_args
        self.msn_peak_picking_args = msn_peak_picking_args

        self.ms1_deconvolution_args = ms1_deconvolution_args
        self.msn_deconvolution_args = msn_deconvolution_args

    @property
    def scan_source(self):
        return self.mzml_file

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

        self._picker_process = ScanIDYieldingProcess(
            self.mzml_file, self._input_queue, start_scan=start_scan, end_scan=end_scan,
            max_scans=max_scans, no_more_event=done_event)
        self._picker_process.start()

        self._deconv_process = ScanTransformingProcess(
            self.mzml_file,
            self._input_queue, self._output_queue, self.averagine, self.charge_range, done_event,
            ms1_peak_picking_args=self.ms1_peak_picking_args, msn_peak_picking_args=self.msn_peak_picking_args,
            ms1_deconvolution_args=self.ms1_deconvolution_args, msn_deconvolution_args=self.msn_deconvolution_args)

        self._deconv_helpers = []

        for i in range(self.number_of_helper_deconvoluters):
            self._deconv_helpers.append(
                ScanTransformingProcess(
                    self.mzml_file,
                    self._input_queue, self._output_queue, self.averagine, self.charge_range,
                    done_event, ms1_peak_picking_args=self.ms1_peak_picking_args,
                    msn_peak_picking_args=self.msn_peak_picking_args,
                    ms1_deconvolution_args=self.ms1_deconvolution_args,
                    msn_deconvolution_args=self.msn_deconvolution_args))
        self._deconv_process.start()

        self._order_manager = ScanOrderManager(
            self._output_queue, done_event, self._deconv_helpers, self._deconv_process)

        for scan in self._order_manager:
            self.time_cache[scan.id] = scan.scan_time
            yield scan
        self.join()
        self._terminate()

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
