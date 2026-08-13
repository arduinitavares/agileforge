- Reject the entire Number List when any parsed value is below zero. The public
  Python operation raises `ValueError` rather than returning a partial sum.
- Format rejection text as `negative numbers not allowed: ` followed by every
  canonical negative value in encounter order, separated by comma and space.
  Preserve duplicate occurrences.
- Install the `string-calculator` command with one positional Number List for
  supported invocations.
- On success, write only the decimal sum and one trailing newline to standard
  output, write nothing to standard error, and exit zero.
- On negative-number rejection, write the Python error text and one trailing
  newline to standard error, write no sum to standard output, and exit nonzero.
