# three.js 0.186.1 (vendored)

Copied from the npm package `three@0.186.1`
(integrity `sha512-blFeqb49wRCSGUGj7gtpfnSGHy2lwDk94RhUmS1c/hTby70kvChbWpkJ4Pm1390LqzzvTmzgXKHPEafJwCb8jA==`),
MIT license (see LICENSE), so the 3D viewer loads nothing from a CDN (rule web.security.headers).

Only the files the viewer needs are kept: `build/three.module.js`, `build/three.core.js`, and the
GLTF and FBX loaders, OrbitControls, RoomEnvironment and their helpers under `examples/jsm/`.

One change from the package: in the `examples/jsm` files, `from 'three'` became a relative path to
`build/three.module.js`, because the CSP forbids the inline import map that a bare `'three'`
would need. Everything else is byte for byte the package's.
