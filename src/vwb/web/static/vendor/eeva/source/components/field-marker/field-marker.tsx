/**
 * Marks a required field: a red asterisk right after the label. Optional
 * fields get no marker at all; there is no other required/optional signal
 * anywhere on the row.
 *
 * @status   draft
 * @use      Right after the label of any required field.
 * @avoid    Not a badge and not a dot. Required is the asterisk, nothing else.
 */
export function FieldMarker() {
	return (
		// Aileron's asterisk sits near the baseline; raised so its top meets cap height, as the boards draw it.
		<span aria-label='required' className='relative -top-[0.21em] ml-0.5 text-[var(--ds-status-danger)]'>
			*
		</span>
	);
}
