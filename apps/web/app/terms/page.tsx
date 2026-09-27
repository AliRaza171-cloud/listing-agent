import type { Metadata } from "next";
import LegalPage, { CONTACT_EMAIL } from "@/components/LegalPage";

export const metadata: Metadata = { title: "Terms of use — Listing Agent" };

export default function TermsPage() {
  return (
    <LegalPage title="Terms of use">
      <p>By creating an account or using Listing Agent you agree to these terms.</p>

      <h2>The service</h2>
      <p>
        Listing Agent writes product listings with AI and publishes them to stores you connect. AI can make mistakes:
        <b> check every listing before publishing it</b>. You are responsible for what you publish in your store,
        including that it is accurate and that you have the right to sell the product and use its photos.
      </p>

      <h2>Your account</h2>
      <p>
        Keep your password safe; you are responsible for activity on your account. Don’t use Listing Agent for
        anything illegal, misleading or harmful, or to publish products you aren’t allowed to sell.
      </p>

      <h2>Credits and payments</h2>
      <p>
        Writing a listing uses credits. If a listing can’t be written, its credit is returned automatically. Credit
        packs are paid through Stripe or Safepay. Unused credits don’t expire while your account is open. Contact us
        if something went wrong with a payment.
      </p>

      <h2>Your content</h2>
      <p>
        Your photos, notes and listings remain yours. You give us permission to process them only to provide the
        service, as described in our <a href="/privacy">privacy policy</a>.
      </p>

      <h2>Availability and changes</h2>
      <p>
        We work to keep Listing Agent available and correct but can’t guarantee it will always be uninterrupted or
        error-free. We may change or end features, and we’ll update these terms if we do; continuing to use the
        service means you accept the updated terms.
      </p>

      <h2>Contact</h2>
      <p>Questions: <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a></p>
    </LegalPage>
  );
}
