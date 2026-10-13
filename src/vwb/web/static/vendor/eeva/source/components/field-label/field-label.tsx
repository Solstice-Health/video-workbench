import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { FieldMarker } from '../field-marker/field-marker';

export type FieldReadiness = 'filled' | 'missing' | 'none';

export interface FieldLabelProps {
	children: React.ReactNode;
	htmlFor?: string;
	required?: boolean;
	/** Required fields only: a check when filled, a dotted ember circle when missing. */
	readiness?: FieldReadiness;
	className?: string;
}

/**
 * A request field's label (Eeva V1 board 3b, from A1 and A5): 13px secondary
 * text, the red asterisk when required, and a readiness mark before the text.
 *
 * @status   draft
 * @variants required; readiness: filled | missing | none
 * @use      Every row of the request panel. The panel's meta line carries the count until the request is ready.
 * @avoid    Optional fields never get a mark. Not a badge or a dot: the check and the dotted circle are the only readiness signals.
 */
export function FieldLabel({ children, htmlFor, required = false, readiness = 'none', className }: FieldLabelProps) {
	const mark = required ? readiness : 'none';
	return (
		<label
			className={cn(
				'whitespace-nowrap text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)] text-[var(--ds-text-secondary)]',
				className
			)}
			htmlFor={htmlFor}
		>
			<span className='inline-flex items-center gap-1.5'>
				{mark === 'filled' ? (
					<Icon className='shrink-0 text-[var(--ds-status-success)] [stroke-width:2.5]' name='check' size={12} />
				) : null}
				{mark === 'missing' ? (
					<span
						aria-hidden='true'
						className='size-3 shrink-0 rounded-full border-[1.5px] border-dotted border-[var(--ds-text-accent)]'
					/>
				) : null}
				{children}
			</span>
			{required ? <FieldMarker /> : null}
		</label>
	);
}
