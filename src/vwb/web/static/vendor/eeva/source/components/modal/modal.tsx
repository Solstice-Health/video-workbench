'use client';

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useRef } from 'react';

import { cn } from '@/lib/utils';

import { Button } from '../button/button';

// Board 8: in base 220 standard, out fast 150 exit; reduced motion is a fast fade.
const MOTION_CLASS = cn(
	'data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:[animation-duration:var(--ds-dur-base)] data-[state=open]:[animation-timing-function:var(--ds-ease-standard)]',
	'data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:[animation-duration:var(--ds-dur-fast)] data-[state=closed]:[animation-timing-function:var(--ds-ease-exit)]',
	'motion-reduce:data-[state=open]:[animation-duration:var(--ds-dur-fast)]'
);

export interface ModalFrameProps extends Omit<React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>, 'asChild'> {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	/** Blurs the page 2px under the scrim, as the brand library does (J1). A confirm dims only (D1). */
	blur?: boolean;
}

/**
 * The scrim and lifted panel every Eeva modal sits in (Eeva V1 board 7): the
 * ink 26% scrim (plus a 2px blur for the brand library), elevation.modal, rising 8px on the way in.
 * Size, radius and layout come from `className`; the panel needs a `DialogPrimitive.Title`.
 *
 * @status   draft
 * @variants none: size it with `className`
 * @use      The base of `Modal` and `LibraryModal`, and of any new modal.
 * @avoid    Use `Modal` for a confirm. Not for a popover or a drawer.
 */
export function ModalFrame({ open, onOpenChange, blur = false, className, children, ...props }: ModalFrameProps) {
	return (
		<DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
			<DialogPrimitive.Portal>
				<DialogPrimitive.Overlay
					className={cn(
						'fixed inset-0 z-[var(--ds-z-modal)] bg-[var(--ds-overlay-scrim)]',
						blur && 'backdrop-blur-[var(--ds-overlay-scrim-blur)]',
						MOTION_CLASS
					)}
				/>
				{/* Centred with inset and auto margins, not a translate, so the rise keyframe owns transform. */}
				<DialogPrimitive.Content
					className={cn(
						'fixed inset-0 z-[var(--ds-z-modal)] m-auto max-h-[calc(100vh-32px)] max-w-[calc(100vw-32px)] bg-[var(--ds-surface-default)] font-[family-name:var(--ds-font-ui)] text-[var(--ds-text-primary)] outline-none [box-shadow:var(--ds-elevation-modal)]',
						'slide-in-from-bottom-[var(--ds-distance-slide)] slide-out-to-bottom-[var(--ds-distance-slide)] motion-reduce:slide-in-from-bottom-0 motion-reduce:slide-out-to-bottom-0',
						MOTION_CLASS,
						className
					)}
					{...props}
				>
					{children}
				</DialogPrimitive.Content>
			</DialogPrimitive.Portal>
		</DialogPrimitive.Root>
	);
}

export interface ModalProps {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	title: string;
	/** The message under the title. */
	children: React.ReactNode;
	/** The safe action, first in the footer. It takes focus and closes. */
	cancelLabel: string;
	confirmLabel: string;
	/** `destructive` when confirming loses work. */
	tone?: 'primary' | 'destructive';
	onConfirm: () => void;
}

/**
 * Eeva's dialog (Eeva V1 board 7, from frame D1): 400px, radius 14, a 17px
 * title, a 14/21 message, and the safe action beside a primary or
 * destructive one. Focus starts on the safe action, so Enter never confirms
 * by accident; Escape and the scrim cancel.
 *
 * @status   draft
 * @variants tone: primary | destructive
 * @use      A question that needs an answer before going on: the major-change confirm (Change and start over).
 * @avoid    Not for picking things; that is `LibraryModal`. Not for news the user does not have to answer.
 */
export function Modal({ open, onOpenChange, title, children, cancelLabel, confirmLabel, tone = 'primary', onConfirm }: ModalProps) {
	const cancelRef = useRef<HTMLButtonElement>(null);
	return (
		<ModalFrame
			className='h-fit w-[400px] rounded-[var(--ds-radius-xl)]'
			open={open}
			role={tone === 'destructive' ? 'alertdialog' : 'dialog'}
			onOpenAutoFocus={event => {
				event.preventDefault();
				cancelRef.current?.focus();
			}}
			onOpenChange={onOpenChange}
		>
			<div className='flex flex-col gap-1.5 px-6 pb-1.5 pt-6'>
				<DialogPrimitive.Title className='m-0 text-[length:var(--ds-type-modal-title-size)] font-semibold leading-[var(--ds-type-modal-title-leading)]'>
					{title}
				</DialogPrimitive.Title>
				<DialogPrimitive.Description asChild>
					<div className='text-[length:var(--ds-type-dialog-body-size)] leading-[var(--ds-type-dialog-body-leading)] text-[var(--ds-text-secondary)]'>
						{children}
					</div>
				</DialogPrimitive.Description>
			</div>
			<div className='flex justify-end gap-2 px-6 pb-[22px] pt-5'>
				<DialogPrimitive.Close asChild>
					<Button ref={cancelRef} variant='secondary'>
						{cancelLabel}
					</Button>
				</DialogPrimitive.Close>
				<Button variant={tone} onClick={onConfirm}>
					{confirmLabel}
				</Button>
			</div>
		</ModalFrame>
	);
}
