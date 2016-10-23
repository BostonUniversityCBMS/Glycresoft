import os
import tempfile

import threading

import logging

try:
    from Queue import Queue, Empty
except ImportError:
    from queue import Queue, Empty

from ms_deisotope.output.db import (
    DatabaseScanSerializer, ScanBunch, DatabaseScanDeserializer,
    FittedPeak, DeconvolutedPeak)
from glycan_profiling.piped_deconvolve import ScanGeneratorBase


class PeakTableIndexToggler(object):
    def __init__(self, session):
        from glycan_profiling.serialize.utils import toggle_indices
        self.session = session
        self.fitted_peak_toggle = toggle_indices(self.session, FittedPeak.__table__)
        self.deconvoluted_peak_toggle = toggle_indices(self.session, DeconvolutedPeak.__table__)

    def drop(self):
        self.fitted_peak_toggle.drop()
        self.deconvoluted_peak_toggle.drop()

    def create(self):
        self.fitted_peak_toggle.create()
        self.deconvoluted_peak_toggle.create()


DONE = b'---NO-MORE---'

logger = logging.getLogger("glycan_profiler.scan_cache")


class ScanCacheHandlerBase(object):
    def __init__(self, *args, **kwargs):
        self.current_precursor = None
        self.current_products = []

    def reset(self):
        self.current_precursor = None
        self.current_products = []

    def save_bunch(self, precursor, products):
        raise NotImplementedError()

    def save(self):
        if self.current_precursor is not None:
            self.save_bunch(
                self.current_precursor, self.current_products)
            self.reset()

    def complete(self):
        pass

    @classmethod
    def configure_storage(cls, path=None, name=None):
        return cls()

    def accumulate(self, scan):
        if self.current_precursor is None:
            self.current_precursor = scan
        elif scan.ms_level == self.current_precursor.ms_level:
            self.save()
            self.current_precursor = scan
        else:
            self.current_products.append(scan)

    def commit(self):
        self.save()
        pass

    def sync(self):
        self.commit()

    def _get_sample_run(self):
        return None

    @property
    def sample_run(self):
        return self._get_sample_run()


class NullScanCacheHandler(ScanCacheHandlerBase):
    def save_bunch(self, precursor, products):
        pass


class DatabaseScanCacheHandler(ScanCacheHandlerBase):
    def __init__(self, connection, sample_name):
        self.serializer = DatabaseScanSerializer(connection, sample_name)
        self.commit_counter = 0
        self.index_controller = PeakTableIndexToggler(self.serializer.session)
        self.index_controller.drop()
        super(DatabaseScanCacheHandler, self).__init__()
        logger.info("Serializing scans under %r @ %r", self.serializer.sample_run, self.serializer.engine)

    def save_bunch(self, precursor, products):
        self.serializer.save(ScanBunch(precursor, products), commit=False)
        self.commit_counter += 1
        if self.commit_counter % 1000 == 0:
            self.commit()

    def commit(self):
        super(DatabaseScanCacheHandler, self).save()
        self.serializer.session.commit()
        self.serializer.session.expunge_all()

    @classmethod
    def configure_storage(cls, path=None, name=None):
        if path is not None:
            if name is None:
                sample_name = os.path.basename(path)
            else:
                sample_name = name
            if not path.endswith(".db") and "://" not in path:
                path = os.path.splitext(path)[0] + '.db'
        elif path is None:
            path = tempfile.mkstemp()[1] + '.db'
            if name is None:
                sample_name = 'sample'
            else:
                sample_name = name
        return cls(path, sample_name)

    def complete(self):
        self.save()
        print("Completing Serializer")
        self.serializer.complete()
        print("Recreating Indices")
        self.index_controller.create()

    def _get_sample_run(self):
        return self.serializer.sample_run


class ThreadedDatabaseScanCacheHandler(DatabaseScanCacheHandler):
    def __init__(self, connection, sample_name):
        super(ThreadedDatabaseScanCacheHandler, self).__init__(connection, sample_name)
        self.queue = Queue()
        self.worker_thread = threading.Thread(target=self._worker_loop)
        self.worker_thread.start()
        self.log_inserts = False

    def _save_bunch(self, precursor, products):
        self.serializer.save(
            ScanBunch(precursor, products), commit=False)

    def save_bunch(self, precursor, products):
        self.queue.put((precursor, products))

    def _worker_loop(self):
        has_work = True
        i = 0
        commit_interval = 1000
        while has_work:
            try:
                next_bunch = self.queue.get(True, 2)
                if next_bunch == DONE:
                    has_work = False
                    continue
                if self.log_inserts:
                    logger.info("Inserting %r", next_bunch)
                self._save_bunch(*next_bunch)
                i += 1

                if i % commit_interval == 0:
                    self.serializer.session.commit()
                    self.serializer.session.expunge_all()
            except Empty:
                continue
                # self.serializer.session.commit()
                # self.serializer.session.expunge_all()
        self.serializer.session.commit()
        self.serializer.session.expunge_all()

    def sync(self):
        self._end_thread()
        self.worker_thread = threading.Thread(target=self._worker_loop)
        self.worker_thread.start()

    def _end_thread(self):
        logger.info(
            "Joining ThreadedDatabaseScanCacheHandler insertion thread. %d work items remaining",
            self.queue.qsize())
        self.log_inserts = True

        self.queue.put(DONE)
        if self.worker_thread is not None:
            self.worker_thread.join()

    def commit(self):
        super(DatabaseScanCacheHandler, self).save()
        self._end_thread()


class DatabaseScanGenerator(ScanGeneratorBase):
    def __init__(self, connection, sample_name):
        self.deserializer = DatabaseScanDeserializer(connection, sample_name)
        self._iterator = None

    def configure_iteration(self, start_scan=None, end_scan=None, max_scans=None):
        self.deserializer.reset()
        self.max_scans = max_scans
        self.start_scan = start_scan
        self.end_scan = end_scan
        self._iterator = self.make_iterator(start_scan, end_scan, max_scans)

    def make_iterator(self, start_scan=None, end_scan=None, max_scans=None):
        index = 0
        if start_scan is not None:
            self.deserializer.start_from_scan(start_scan)

        count = 0
        if max_scans is None:
            max_scans = float('inf')

        while count < max_scans:
            try:
                scan, products = next(self.deserializer)
                scan_id = scan.id
                if scan_id == end_scan:
                    break
                yield scan
                for prod in products:
                    yield prod

                index += 1
                count += 1
            except Exception:
                logger.exception("An error occurred while fetching scans", exc_info=True)
                break

    def convert_scan_id_to_retention_time(self, scan_id):
        return self.deserializer.convert_scan_id_to_retention_time(scan_id)
