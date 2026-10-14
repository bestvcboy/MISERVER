import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class ObservationDuration:
    minimum_seconds: int = 480

    def __post_init__(self):
        if type(self.minimum_seconds) is not int or not 480 <= self.minimum_seconds <= 570:
            raise ValueError('Minimum observation must be an integer from480 through570 seconds')

    @property
    def status_samples(self):
        return (self.minimum_seconds + 2) // 3 + 2

    @property
    def history_min_samples(self):
        return self.status_samples - 1

    @property
    def nominal_span(self):
        return 3 * (self.status_samples - 1)

    @property
    def outer_limit(self):
        return 600 if self.minimum_seconds == 480 else 720

    @property
    def minimum_launch_remaining(self):
        return self.nominal_span + 60


def minimum_observation_argument(value):
    if not isinstance(value, str) or not value or any(character not in '0123456789' for character in value):
        raise argparse.ArgumentTypeError('Minimum observation must be ASCII decimal seconds from480 through570')
    try:
        return ObservationDuration(int(value)).minimum_seconds
    except ValueError:
        raise argparse.ArgumentTypeError('Minimum observation must be ASCII decimal seconds from480 through570')
