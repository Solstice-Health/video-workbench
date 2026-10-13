import { cn } from '@/lib/utils';

import { fieldRingClass } from '../text-field/text-field';

// The board 3b field with an icon: the ring sits on the shell and follows the
// input inside it, so hover and editing look the same as a bare TextField.
export function FieldShell({
	children,
	error = false,
	tall = false,
	className
}: {
	children: React.ReactNode;
	error?: boolean;
	/** 40px instead of 32px. Search only. */
	tall?: boolean;
	className?: string;
}) {
	return (
		<div
			className={cn(
				'flex w-full items-center gap-2 rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] px-2.5 transition-shadow [transition-duration:var(--ds-dur-fast)] [transition-timing-function:var(--ds-ease-standard)]',
				tall ? 'h-[var(--ds-size-md)]' : 'h-[var(--ds-size-sm)]',
				fieldRingClass(error),
				className
			)}
		>
			{children}
		</div>
	);
}

export const FIELD_INPUT_CLASS =
	'h-full min-w-0 flex-1 truncate bg-transparent text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none placeholder:text-[var(--ds-text-tertiary)]';

export const FIELD_ICON_CLASS = 'shrink-0 text-[var(--ds-text-tertiary)]';
