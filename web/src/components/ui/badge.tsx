import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] leading-4 font-medium whitespace-nowrap [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        outline: "border-border text-muted-foreground",
        pass: "border-[color-mix(in_oklch,var(--pass)_45%,transparent)] bg-[color-mix(in_oklch,var(--pass)_16%,transparent)] text-[var(--pass)]",
        fail: "border-[color-mix(in_oklch,var(--fail)_45%,transparent)] bg-[color-mix(in_oklch,var(--fail)_16%,transparent)] text-[var(--fail)]",
        warn: "border-[color-mix(in_oklch,var(--warn)_45%,transparent)] bg-[color-mix(in_oklch,var(--warn)_16%,transparent)] text-[var(--warn)]",
        info: "border-[color-mix(in_oklch,var(--info)_45%,transparent)] bg-[color-mix(in_oklch,var(--info)_16%,transparent)] text-[var(--info)]",
        neutral: "border-border bg-muted text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

function Badge({
  className,
  variant,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : "span";
  return <Comp data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
