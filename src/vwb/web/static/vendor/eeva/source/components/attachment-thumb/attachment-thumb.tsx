'use client';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { Tooltip } from '../tooltip/tooltip';

export type AttachmentFileType = 'pdf' | 'img' | 'doc';
export type AttachmentStatus = 'ready' | 'uploading' | 'failed';

const BADGE: Record<AttachmentFileType, { label: string; className: string }> = {
	pdf: { label: 'PDF', className: 'bg-[var(--ds-file-badge-pdf-bg)] text-[var(--ds-file-badge-pdf)]' },
	img: { label: 'IMG', className: 'bg-[var(--ds-file-badge-img-bg)] text-[var(--ds-file-badge-img)]' },
	doc: { label: 'DOC', className: 'bg-[var(--ds-file-badge-doc-bg)] text-[var(--ds-file-badge-doc)]' }
};

export interface AttachmentThumbProps {
	name: string;
	fileType: AttachmentFileType;
	status?: AttachmentStatus;
	/** 0 to 100 while uploading: the 2px bar along the bottom. Unset while the server reads the file. */
	progress?: number;
	/** Failed: why, as the thumb's hover title (no meta line; the boards draw none). */
	failure?: string;
	/** Staged in the composer: the × at the end (M5, M6). Its tooltip reads Remove file. */
	onRemove?: () => void;
	/** The badge and name open the file (a viewer); the × stays separate. */
	onOpen?: () => void;
	className?: string;
}

/**
 * One attached file (Eeva V1 board 4, from A1, M5, M6): a file-type badge and
 * the name on white, 32px. Above a sent message it carries a soft edge; staged
 * in the composer it takes a firmer edge, an ×, and while uploading a 2px
 * progress bar along the bottom.
 *
 * @status   draft
 * @variants status: ready | uploading | failed; removable (staged); openable
 * @use      Above a sent message in `UserMessage`, and staged in the Composer's `attachments` slot with `onRemove`. `onOpen` to preview it.
 * @avoid    Not a link or a tag. A file in the request panel is a reference-file row.
 */
export function AttachmentThumb({
	name,
	fileType,
	status = 'ready',
	progress,
	failure,
	onRemove,
	onOpen,
	className
}: AttachmentThumbProps) {
	const failed = status === 'failed';
	const uploading = status === 'uploading';
	const badge = BADGE[fileType];
	const Body = onOpen ? 'button' : 'span';

	return (
		<div
			aria-busy={uploading || undefined}
			className={cn(
				'relative inline-flex h-8 max-w-[240px] shrink-0 items-center gap-2 overflow-hidden rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] py-1 pl-1',
				onRemove ? 'pr-2' : 'pr-2.5',
				failed
					? '[box-shadow:0_0_0_1px_var(--ds-status-danger-edge)]'
					: onRemove
						? '[box-shadow:var(--ds-elevation-thumb-staged)]'
						: '[box-shadow:var(--ds-elevation-thumb)]',
				className
			)}
			title={failed ? (failure ?? "Couldn't read this file") : undefined}
		>
			<Body
				aria-label={onOpen ? `Open ${name}` : undefined}
				className={cn(
					'flex min-w-0 flex-1 items-center gap-2 text-left',
					onOpen && 'rounded-[var(--ds-radius-sm)] outline-none focus-visible:[box-shadow:var(--ds-focus-ring)]'
				)}
				type={onOpen ? 'button' : undefined}
				onClick={onOpen}
			>
				<span
					aria-hidden='true'
					className={cn(
						'flex size-6 shrink-0 items-center justify-center rounded-[var(--ds-radius-sm)] text-[8px] font-bold',
						failed ? 'bg-[var(--ds-status-danger-bg)] text-[var(--ds-status-danger)]' : badge.className
					)}
				>
					{failed ? <Icon name='circle-alert' size={12} /> : badge.label}
				</span>
				<span
					className={cn(
						'truncate text-[length:var(--ds-type-ui-size)] font-medium leading-[var(--ds-type-ui-leading)]',
						failed ? 'text-[var(--ds-status-danger)]' : 'text-[var(--ds-text-primary)]'
					)}
					title={failed ? undefined : name}
				>
					{name}
				</span>
			</Body>
			{onRemove ? (
				<Tooltip content='Remove file'>
					<button
						aria-label={`Remove ${name}`}
						className='-mr-0.5 flex size-5 shrink-0 items-center justify-center rounded-[var(--ds-radius-pill)] text-[var(--ds-text-tertiary)] outline-none transition-colors [transition-duration:var(--ds-dur-instant)] hover:text-[var(--ds-text-primary)] focus-visible:[box-shadow:var(--ds-focus-ring)]'
						type='button'
						onClick={onRemove}
					>
						<Icon name='x' size={12} />
					</button>
				</Tooltip>
			) : null}
			{uploading ? (
				// M5: ink on ink 8%. While the server reads (no progress yet) the bar is full and breathes.
				<span aria-hidden='true' className='absolute inset-x-0 bottom-0 h-0.5 bg-[var(--ds-upload-track)]'>
					<span
						className={cn(
							'block h-full bg-[var(--ds-text-primary)] transition-[width] [transition-duration:var(--ds-dur-fast)]',
							progress === undefined && 'animate-pulse motion-reduce:animate-none'
						)}
						style={{ width: `${progress ?? 100}%` }}
					/>
				</span>
			) : null}
		</div>
	);
}
