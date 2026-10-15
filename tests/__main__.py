"""Allows `python -m tests` (README) as well as discover."""
import unittest

if __name__ == '__main__':
    unittest.main(module=None, argv=['python -m tests', 'discover', '-s', 'tests', '-v'])
