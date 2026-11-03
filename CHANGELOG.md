# Changelog

## [0.1.1](https://github.com/arnabdas24/aws-sso-mcp/compare/v0.1.0...v0.1.1) (2026-11-03)


### Bug Fixes

* **ci:** pin pypi-publish to the commit SHA, not the tag object ([#3](https://github.com/arnabdas24/aws-sso-mcp/issues/3)) ([87af77d](https://github.com/arnabdas24/aws-sso-mcp/commit/87af77d84e28b48f0f1033e3b69a7e685ff148fc))

## 0.1.0 (2026-11-03)


### Features

* fan-out progress notifications, fail-fast read-only gate, config example ([2d61ca7](https://github.com/arnabdas24/aws-sso-mcp/commit/2d61ca7c81e033e03367008241f90e29a4b402f8))
* generic query engine, role resolution, multi-step DAG (Phase 2-4) ([af0bdd3](https://github.com/arnabdas24/aws-sso-mcp/commit/af0bdd3dd267b14a9db9c03bccb5c32b1c5aaba8))
* scaffold standalone aws-sso-mcp server (Phase 0 + Phase 1) ([b079d4a](https://github.com/arnabdas24/aws-sso-mcp/commit/b079d4a16ec0dbc5a55a2c84079c258f82cc96b1))


### Bug Fixes

* **cache:** guard role grants against a mid-fetch refresh and lock counters ([4851b5a](https://github.com/arnabdas24/aws-sso-mcp/commit/4851b5ad9c6ae9be45a13c4331604938d5059d1b))
* **errors:** explain how to fix a TLS-interception failure ([8d552fa](https://github.com/arnabdas24/aws-sso-mcp/commit/8d552fad55766acbe9b41bccb5cd904f577e3140))
* **hooks:** run the mypy pre-commit hook through uv ([035efbd](https://github.com/arnabdas24/aws-sso-mcp/commit/035efbd96641bad339b217d095cac6613a59e93d))
* keep __version__ in step with the packaged version ([1642083](https://github.com/arnabdas24/aws-sso-mcp/commit/16420832030ac163d168713cd5b93005506d55ad))
* redact rendered logs, pre-flight tokens before planning, bound per-target timeouts ([7b17c72](https://github.com/arnabdas24/aws-sso-mcp/commit/7b17c7269ce875d07979cea2edbf206cb9ddad2d))
* **sso:** tell the user to open the sign-in link manually when no browser opens ([d6e386b](https://github.com/arnabdas24/aws-sso-mcp/commit/d6e386b32b42c14a3e8050580fc326f137cea282))
* **sso:** write the token cache atomically and refuse near-expiry tokens ([ef31e63](https://github.com/arnabdas24/aws-sso-mcp/commit/ef31e63e821924b0ba915dd8c7137655644a3cca))


### Documentation

* add a setup guide and worked examples ([47cca80](https://github.com/arnabdas24/aws-sso-mcp/commit/47cca8042758539b8ece5e9a9f2436b646a221cd))
* add examples/ folder with config example and run-it README ([d7449b4](https://github.com/arnabdas24/aws-sso-mcp/commit/d7449b430a01aacea7cc18a78092c6e73810ba88))
* add the documentation set, ADRs, SECURITY and CONTRIBUTING ([1abf2ad](https://github.com/arnabdas24/aws-sso-mcp/commit/1abf2add50df8c9d9b277d87d26d661bf9ef23da))
* add VS Code MCP setup instructions to examples/README ([4d56b63](https://github.com/arnabdas24/aws-sso-mcp/commit/4d56b633b7bee5f9165b934b60b83418e8e82c4b))
* clarify step 4 is for Claude Desktop/generic clients, not VS Code ([4a37f07](https://github.com/arnabdas24/aws-sso-mcp/commit/4a37f07e9627154ec535ed42cf0ae34fc5fd2db0))
* document global install (uv tool install) as the primary run path ([79d1c51](https://github.com/arnabdas24/aws-sso-mcp/commit/79d1c51316ec0a9a81e052102728ccc253ccc6ca))
* update example config and run instructions ([6eccd86](https://github.com/arnabdas24/aws-sso-mcp/commit/6eccd864db23f1a49fef269ee96725436ac07c23))


### Code Refactoring

* read __version__ from package metadata ([f5946fa](https://github.com/arnabdas24/aws-sso-mcp/commit/f5946faab7c49ecfa3c29d6098837d9cd5a63608))
