import { Suspense } from "react";
import type { Metadata } from "next";
import { ChatApp, ChatSkeleton } from "@/components/chat/ChatApp";

export const metadata: Metadata = {
  title: "SPOTbot — SPOTlight",
  description: "Ask about SPOT missions, assets and flags; every answer cites the mission data it used.",
};

/* The chat lives in the layout, not the pages. /assistant and
   /assistant/[conversationId] share this layout, so moving between them —
   including the router.replace that gives a brand-new chat its URL while its
   first answer is still streaming — never unmounts the chat UI. The pages
   exist only so both URLs route here.

   ChatApp reads usePathname(). For /assistant/[conversationId] the id isn't
   known at build time, so with Cache Components the hook suspends while the
   static shell is prerendered; the Suspense boundary keeps that to this
   subtree and puts a skeleton in the shell. */
export default function AssistantLayout({ children }: LayoutProps<"/assistant">) {
  return (
    <>
      <Suspense fallback={<ChatSkeleton />}>
        <ChatApp />
      </Suspense>
      {children}
    </>
  );
}
