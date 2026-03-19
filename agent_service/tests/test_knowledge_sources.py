from __future__ import annotations

import unittest

from mina_agent.knowledge.sources import HtmlTextExtractor


class KnowledgeSourcesTests(unittest.TestCase):
    def test_extractor_prefers_main_content_and_skips_nav_noise(self) -> None:
        html = """
        <html>
          <head><title>Example Page</title></head>
          <body>
            <div class="site-header">Header Noise</div>
            <nav>Nav Noise</nav>
            <main class="page-content">
              <article>
                <h1>Useful Title</h1>
                <p>Main paragraph one.</p>
                <div class="toc">TOC Noise</div>
                <p>Main paragraph two.</p>
                <a href="/w/Linked_Page">Linked Page</a>
              </article>
            </main>
            <div class="footer-links">Footer Noise</div>
          </body>
        </html>
        """

        extractor = HtmlTextExtractor("https://minecraft.wiki/w/Example")
        extractor.feed(html)
        title, content, links = extractor.build()

        self.assertEqual(title, "Example Page")
        self.assertIn("Useful Title", content)
        self.assertIn("Main paragraph one.", content)
        self.assertIn("Main paragraph two.", content)
        self.assertNotIn("Header Noise", content)
        self.assertNotIn("Nav Noise", content)
        self.assertNotIn("TOC Noise", content)
        self.assertNotIn("Footer Noise", content)
        self.assertIn("https://minecraft.wiki/w/Linked_Page", links)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
