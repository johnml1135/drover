"""Windows CPU load reading: GetSystemTimes samples mapped onto the per-core load thresholds."""
import unittest
from unittest import mock

from drover import resources


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class WindowsLoad(unittest.TestCase):
    def setUp(self):
        resources._CPU.update(last=None, busy=None)
        self.clock = Clock()
        self.idle = self.kernel = self.user = 0

    def advance(self, seconds, busy):
        """Add `seconds` of wall time at the given busy fraction on one core (kernel includes idle)."""
        ticks = int(seconds * 10_000_000)
        self.clock.now += seconds
        self.idle += int(ticks * (1 - busy))
        self.kernel += int(ticks * (1 - busy))
        self.user += int(ticks * busy)

    def read(self):
        return resources._windows_cpu_busy(times=lambda: (self.idle, self.kernel, self.user), clock=self.clock,
                                           sleep=lambda s: self.advance(s, 0.5))

    def test_first_read_samples_its_own_window(self):
        self.assertAlmostEqual(self.read(), 0.5, places=2)

    def test_busy_is_smoothed_between_ticks(self):
        self.read()
        self.advance(60, 1.0)
        busy = self.read()
        self.assertGreater(busy, 0.5)
        self.assertLess(busy, 0.9)   # one time constant moves about 63% of the way
        for _ in range(10):
            self.advance(60, 1.0)
            busy = self.read()
        self.assertGreater(busy, 0.99)

    def test_unavailable_counter_is_unknown(self):
        self.assertIsNone(resources._windows_cpu_busy(times=lambda: None, clock=self.clock))

    def test_windows_without_loadavg_maps_busy_onto_thresholds(self):
        for busy, expected in ((0.5, 'go'), (0.8, 'queue'), (0.99, 'hold')):
            with mock.patch.object(resources, 'is_windows', return_value=True), \
                    mock.patch.object(resources, '_windows_cpu_busy', return_value=busy), \
                    mock.patch('os.cpu_count', return_value=8), \
                    mock.patch.object(resources.Path, 'read_text', side_effect=OSError), \
                    mock.patch('os.getloadavg', side_effect=OSError, create=True):
                load5, cores = resources.read_load()
                self.assertEqual(cores, 8)
                per_core = load5 / cores
                got = 'hold' if per_core > 3.5 else 'queue' if per_core > 2.5 else 'go'
                self.assertEqual(got, expected, busy)

    def test_non_windows_without_loadavg_stays_unknown(self):
        with mock.patch.object(resources, 'is_windows', return_value=False), \
                mock.patch.object(resources.Path, 'read_text', side_effect=OSError), \
                mock.patch('os.getloadavg', side_effect=OSError, create=True):
            self.assertIsNone(resources.read_load())

    def test_linux_loadavg_unchanged(self):
        with mock.patch.object(resources.Path, 'read_text', return_value='0.1 2.5 3.0 1/2 3'), \
                mock.patch('os.cpu_count', return_value=4):
            self.assertEqual(resources.read_load(), (2.5, 4))


if __name__ == '__main__':
    unittest.main()
