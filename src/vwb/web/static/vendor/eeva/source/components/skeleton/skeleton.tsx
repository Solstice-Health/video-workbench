import { cn } from '@/lib/utils';

export type SkeletonShape = 'field' | 'notes';

// As drawn: a field value is 32px at radius 8; inside Notes it is 78px at radius 6 (A1).
const SHAPE_CLASS: Record<SkeletonShape, string> = {
	field: 'h-[var(--ds-size-sm)] rounded-[var(--ds-radius-md)]',
	notes: 'h-[78px] rounded-[var(--ds-radius-sm)]'
};

export interface SkeletonProps {
	shape?: SkeletonShape;
	className?: string;
}

/**
 * A placeholder bar while a value loads (Eeva V1 boards 5 and 1c). Ink 6%
 * with a 90% white band sweeping every 3.2s; the band is fixed to the
 * viewport, so every bar on screen shares one wave.
 *
 * @status   draft
 * @variants shape: field | notes
 * @use      In place of a field value while the agent prepares or updates it (A1, C1), and inside Notes. The label stays.
 * @avoid    Never in place of a label or a heading. Never gray blocks for a whole layout: only the values that are on their way.
 */
export function Skeleton({ shape = 'field', className }: SkeletonProps) {
	return (
		<span
			aria-hidden='true'
			className={cn(
				'block w-full animate-ds-skeleton bg-[var(--ds-skeleton-fill)] bg-[linear-gradient(100deg,rgba(255,255,255,0)_20%,var(--ds-skeleton-sheen)_50%,rgba(255,255,255,0)_80%)] bg-[length:200%_100%] bg-fixed bg-no-repeat motion-reduce:animate-none',
				SHAPE_CLASS[shape],
				className
			)}
		/>
	);
}
