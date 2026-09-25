# Bundled fonts

**orbitron-700.woff2** - Orbitron Bold, used only for the CRYPTANET wordmark in
the sidebar.

* Designer: Matt McInerney. Licence: SIL Open Font License 1.1, which permits
  bundling and redistribution: https://openfontlicense.org
* Source: the Latin subset Google Fonts serves for
  `https://fonts.googleapis.com/css2?family=Orbitron:wght@700`
* Shipped as a file rather than loaded from Google at runtime. The dashboard has
  to render identically offline and on an air-gapped demo machine, and a
  wordmark that silently falls back to Arial on the demo laptop is exactly the
  failure that rule exists to prevent.
