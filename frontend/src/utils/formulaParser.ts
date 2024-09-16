import { evaluate, parse } from 'mathjs';
import { FormulaSchema } from 'backend/app/schema/workbook_schema';

// HUMAN ASSISTANCE NEEDED
// The following function may need additional error handling and edge case considerations
export function parseFormula(formula: string): FormulaSchema {
  // Remove leading '=' if present
  const cleanFormula = formula.startsWith('=') ? formula.slice(1) : formula;

  try {
    // Use mathjs to parse the formula string
    const parsedMath = parse(cleanFormula);

    // Convert parsed result to FormulaSchema structure
    // Note: This conversion is simplified and may need to be expanded based on the exact structure of FormulaSchema
    const formulaSchema: FormulaSchema = {
      expression: parsedMath.toString(),
      variables: parsedMath.variables(),
      // Add other properties as needed by FormulaSchema
    };

    return formulaSchema;
  } catch (error) {
    console.error('Error parsing formula:', error);
    throw new Error('Failed to parse formula');
  }
}

// HUMAN ASSISTANCE NEEDED
// The following function requires more robust error handling and type checking
export function evaluateFormula(parsedFormula: FormulaSchema, cellValues: Record<string, any>): any {
  try {
    // Replace cell references with actual values from cellValues
    let formulaWithValues = parsedFormula.expression;
    for (const variable of parsedFormula.variables) {
      if (cellValues.hasOwnProperty(variable)) {
        formulaWithValues = formulaWithValues.replace(new RegExp(variable, 'g'), cellValues[variable].toString());
      } else {
        throw new Error(`Missing value for cell reference: ${variable}`);
      }
    }

    // Use mathjs to evaluate the formula
    const result = evaluate(formulaWithValues);

    return result;
  } catch (error) {
    console.error('Error evaluating formula:', error);
    throw new Error('Failed to evaluate formula');
  }
}