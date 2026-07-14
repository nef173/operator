import { redirect } from "next/navigation";

// The Assistant now lives inside the Decisions tab — steering by suggestion and steering
// by chat are the same act, so they share one page. Old links land there.
export default function AssistantPage() {
  redirect("/decisions");
}
