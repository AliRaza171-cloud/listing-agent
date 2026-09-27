import type { Metadata } from "next";
import LegalPage, { CONTACT_EMAIL } from "@/components/LegalPage";

export const metadata: Metadata = { title: "Privacy policy — Listing Agent" };

export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy policy">
      <p>
        Listing Agent helps online sellers turn product photos, notes and voice notes into product listings and
        publish them to their own stores. This page explains what we collect, why, and the choices you have.
      </p>

      <h2>What we collect</h2>
      <ul>
        <li><b>Your account:</b> email address, name (optional) and a securely hashed password.</li>
        <li><b>What you give us to write listings:</b> product photos, notes, voice recordings, and the listings,
          prices and stock details you create or edit.</li>
        <li><b>Store connections:</b> your store’s address and the access keys or tokens needed to publish products.
          These are encrypted as soon as they reach us and are never shown again, not even to you.</li>
        <li><b>Credits and payments:</b> your credit balance and a record of each purchase (pack, amount, status).
          Card and wallet details are entered on Stripe’s or Safepay’s own pages — we never see or store them.</li>
        <li><b>Technical logs:</b> request logs (such as time and error messages) kept to run and secure the service.</li>
      </ul>

      <h2>Shopify, WooCommerce and other stores</h2>
      <p>
        When you connect a store, Listing Agent only asks for the access it needs to publish your products: creating
        and updating products, their photos, stock and collections/categories. <b>We do not request or store your
        customers’ personal data, orders or payment information.</b> If you remove Listing Agent from your store
        (for example, uninstall the Shopify app), we disconnect it and delete its access token. When Shopify asks us
        to erase a shop’s data, we delete that shop’s connection from our systems.
      </p>

      <h2>How we use your data</h2>
      <ul>
        <li>To write listings: photos, notes and voice notes are sent to our AI provider (Google Gemini or OpenAI)
          to analyse the product and write the text. They are used only to produce your listing.</li>
        <li>To publish products to the stores you connected, when you ask us to.</li>
        <li>To manage your credits and process purchases through Stripe or Safepay.</li>
        <li>To keep the service secure and working, and to answer your support requests.</li>
      </ul>
      <p>We don’t sell your data and don’t use it for advertising.</p>

      <h2>Who we share it with</h2>
      <p>Only the services needed to run Listing Agent:</p>
      <ul>
        <li>AI providers (Google Gemini or OpenAI) — photos, notes and voice notes, to write listings.</li>
        <li>Payment providers (Stripe, Safepay) — to take payments for credits.</li>
        <li>Your connected stores (Shopify, WooCommerce, your own site) — the products you publish.</li>
        <li>Our hosting provider, which stores the data on our behalf.</li>
      </ul>

      <h2>How long we keep it</h2>
      <p>
        We keep your account data while your account is open. Deleting a product removes it and its listings;
        disconnecting a store deletes its saved keys. If you ask us to delete your account, we delete your data
        within 30 days, except payment records we must keep for accounting.
      </p>

      <h2>Your choices</h2>
      <p>
        You can see and edit your listings, disconnect stores and delete products in the app at any time. To get a
        copy of your data or delete your account, email us at <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>

      <h2>Security</h2>
      <p>
        Connections to Listing Agent use HTTPS. Store keys are encrypted at rest, passwords are hashed, and our
        services are not reachable from the internet except through our secured API.
      </p>

      <h2>Changes and contact</h2>
      <p>
        If we change this policy we’ll update the date above. Questions? Write to{" "}
        <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>
    </LegalPage>
  );
}
