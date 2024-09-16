import { format } from 'date-fns';

export function formatCellValue(value: any, formatString: string): string {
  if (typeof value === 'number') {
    if (formatString.includes('%')) {
      return (value * 100).toFixed(2) + '%';
    }
    if (formatString.includes('$')) {
      return '$' + value.toFixed(2);
    }
    return value.toString();
  }

  if (value instanceof Date) {
    return format(value, formatString);
  }

  return String(value);
}

// HUMAN ASSISTANCE NEEDED
// This function requires a more complex implementation to handle all Excel format string cases
export function parseFormatString(formatString: string): object {
  // Basic implementation, needs to be expanded
  const sections = formatString.split(';');
  const parsedFormat: { [key: string]: string } = {};

  if (sections.length >= 1) parsedFormat.positive = sections[0];
  if (sections.length >= 2) parsedFormat.negative = sections[1];
  if (sections.length >= 3) parsedFormat.zero = sections[2];
  if (sections.length >= 4) parsedFormat.text = sections[3];

  return parsedFormat;
}