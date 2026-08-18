"""The agreed cutting coefficients — the single source for this calculator.

Nothing here is stored: the numbers are business constants, so they live in
code, are rendered into the page by the view, and the browser reads the
coefficient of a package straight off the selected option. Neither the
template nor `static/js/plate_cutting.js` restates a value.

`Decimal` on purpose: the coefficients are exact two-decimal figures and must
survive rendering as such. The arithmetic itself happens in the browser.
"""
from dataclasses import dataclass
from decimal import Decimal

EN_DASH = '–'

#: One hole always costs this many seconds, whatever the plate length is.
HOLE_SECONDS = Decimal('0.95')


@dataclass(frozen=True)
class PlateLengthRange:
    """One plate-length band and the seconds one plate of it takes to cut."""

    min_mm: int
    max_mm: int
    seconds: Decimal

    @property
    def value(self):
        """The stable identifier of the band in the page's `<select>`."""
        return str(self.min_mm)

    @property
    def label(self):
        return f'{self.min_mm}{EN_DASH}{self.max_mm} мм'


#: The seventeen agreed bands, ascending and contiguous.
PLATE_LENGTH_RANGES = tuple(
    PlateLengthRange(min_mm, max_mm, Decimal(seconds))
    for min_mm, max_mm, seconds in (
        (1, 170, '0.74'),
        (171, 340, '0.91'),
        (341, 510, '1.09'),
        (511, 680, '1.27'),
        (681, 850, '1.43'),
        (851, 1020, '1.60'),
        (1021, 1190, '1.78'),
        (1191, 1360, '1.93'),
        (1361, 1530, '2.11'),
        (1531, 1700, '2.29'),
        (1701, 1870, '2.47'),
        (1871, 2040, '2.65'),
        (2041, 2210, '2.83'),
        (2211, 2380, '3.01'),
        (2381, 2550, '3.19'),
        (2551, 2720, '3.37'),
        (2721, 2890, '3.55'),
    )
)
