import unittest
from calculation_engine import CalculationEngine

class TestCalculationEngine(unittest.TestCase):
    def setUp(self):
        self.calc_engine = CalculationEngine()

    def test_formula_parsing(self):
        self.assertEqual(self.calc_engine.parse_formula("=A1+B2"), ["A1", "+", "B2"])
        self.assertEqual(self.calc_engine.parse_formula("=SUM(A1:A10)"), ["SUM", "(", "A1:A10", ")"])
        self.assertEqual(self.calc_engine.parse_formula("=IF(A1>0,B1,C1)"), ["IF", "(", "A1", ">", "0", ",", "B1", ",", "C1", ")"])

    def test_basic_arithmetic(self):
        self.assertEqual(self.calc_engine.calculate("=1+2"), 3)
        self.assertEqual(self.calc_engine.calculate("=10-5"), 5)
        self.assertEqual(self.calc_engine.calculate("=3*4"), 12)
        self.assertEqual(self.calc_engine.calculate("=20/5"), 4)
        self.assertEqual(self.calc_engine.calculate("=2^3"), 8)

    def test_complex_excel_functions(self):
        # Mocking cell values for testing
        self.calc_engine.set_cell_value("A1", 10)
        self.calc_engine.set_cell_value("A2", 20)
        self.calc_engine.set_cell_value("A3", 30)

        self.assertEqual(self.calc_engine.calculate("=SUM(A1:A3)"), 60)
        self.assertEqual(self.calc_engine.calculate("=AVERAGE(A1:A3)"), 20)
        self.assertEqual(self.calc_engine.calculate("=MAX(A1:A3)"), 30)
        self.assertEqual(self.calc_engine.calculate("=MIN(A1:A3)"), 10)
        self.assertEqual(self.calc_engine.calculate("=IF(A1>15,A2,A3)"), 30)

    def test_error_handling(self):
        with self.assertRaises(ValueError):
            self.calc_engine.calculate("=1/0")
        
        with self.assertRaises(ValueError):
            self.calc_engine.calculate("=SQRT(-1)")
        
        with self.assertRaises(SyntaxError):
            self.calc_engine.calculate("=SUM(A1:A3")
        
        # HUMAN ASSISTANCE NEEDED
        # Additional error handling cases might be needed depending on the specific implementation of the CalculationEngine
        # Consider adding tests for:
        # - Invalid cell references
        # - Circular references
        # - Unsupported functions
        # - Type mismatches (e.g., string in numeric operation)

if __name__ == '__main__':
    unittest.main()