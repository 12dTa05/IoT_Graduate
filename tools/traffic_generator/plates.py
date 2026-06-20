"""Vietnamese license plate generator.

Generates standard modern civilian vehicle registration plates.
Format: XXY-123.45 where:
  XX = region code (e.g., 30, 51, 59)
  Y = letter (A-Z excluding O, I, Q)
  123 = 3 digits
  45 = 2 digits

Older format: XX-12345 (also supported)
"""

import random

# Vietnamese region codes (province/city codes)
REGIONS = [
    '29', '30', '31', '33',  # Hanoi variants
    '43', '51', '53', '59',  # Ho Chi Minh variants
    '60', '61', '75', '77',  # Da Nang, Can Tho, etc.
]

# Allowed letters (excluding O, I, Q which are not used in civilian plates)
ALPHABET = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'K', 'L', 'M', 'N', 'P', 'R', 'S', 'T', 'U', 'V', 'X', 'Y']

def generate_vietnamese_plate(new_format=True) -> str:
    """Generate a realistic Vietnamese license plate number.

    Args:
        new_format: If True, generates new style: XXY-123.45
                   If False, generates old style: XX-12345

    Returns:
        Plate string like "30K-123.45" or "30-12345"
    """
    region = random.choice(REGIONS)

    if new_format:
        # New format: 1 letter after region
        letter = random.choice(ALPHABET)
        num1 = f"{random.randint(100, 999):03d}"
        num2 = f"{random.randint(10, 99):02d}"
        return f"{region}{letter}-{num1}.{num2}"
    else:
        # Old format: 5 digits only
        numbers = f"{random.randint(10000, 99999):05d}"
        return f"{region}-{numbers}"

def generate_plate_batch(n: int, new_format=True) -> list:
    """Generate n unique license plates."""
    plates = set()
    while len(plates) < n:
        plates.add(generate_vietnamese_plate(new_format))
    return list(plates)
