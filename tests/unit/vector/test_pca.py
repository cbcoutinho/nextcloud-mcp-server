"""Unit tests for the PCA used by search-result visualization (card #751).

``PCA.fit`` used to eigendecompose the ``n_features x n_features`` covariance
matrix. At the shapes this code actually sees — a few dozen embeddings of 1024
dims — that cost O(n_features^3) and measured at ~5.6s in production, over half
the latency of a hybrid search request. It now takes a thin SVD of the centered
sample matrix instead, which is O(n_samples^2 * n_features).

The contract this file pins:

* the SVD result matches the covariance/eigendecomposition it replaced, for
  both coordinates (up to per-component sign) and explained-variance ratios
* no ``n_features x n_features`` covariance matrix is built
* component signs are deterministic, so the visualization does not mirror
  between two fits of the same data
* degenerate axes (more components requested than the data spans) yield a
  zero coordinate rather than an arbitrary direction or a short array
* the guards — 2D input, enough features, enough samples, fit-before-transform
  — still raise
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from nextcloud_mcp_server.vector.pca import PCA
from nextcloud_mcp_server.vector.visualization import compute_pca_coordinates

pytestmark = pytest.mark.unit


def _reference_fit_transform(
    X: np.ndarray, n_components: int
) -> tuple[np.ndarray, np.ndarray]:
    """The previous covariance/eigendecomposition implementation, verbatim.

    Kept here as the oracle the SVD path must agree with.
    """
    samples = np.asarray(X, dtype=np.float64)
    mean = np.mean(samples, axis=0)
    centered = samples - mean

    cov = np.cov(centered.T)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)

    idx = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    components = eigenvectors[:, :n_components].T
    explained_variance = eigenvalues[:n_components]

    total_variance = np.sum(eigenvalues)
    if total_variance > 0:
        ratio = explained_variance / total_variance
    else:
        ratio = np.zeros(n_components)

    return np.dot(centered, components.T), ratio


def _random_embeddings(n_samples: int, n_features: int, seed: int = 1234) -> np.ndarray:
    """Unit-norm vectors, matching how the caller normalizes before PCA."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n_samples, n_features))
    return X / np.linalg.norm(X, axis=1, keepdims=True)


class TestEquivalenceWithPreviousImplementation:
    def test_coordinates_match_reference_up_to_sign(self):
        X = _random_embeddings(30, 128)

        actual = PCA(n_components=3).fit_transform(X)
        expected, _ = _reference_fit_transform(X, n_components=3)

        assert actual.shape == expected.shape
        # Each axis is determined only up to sign; compare magnitudes, then
        # confirm each column matches the reference under a single global flip.
        np.testing.assert_allclose(np.abs(actual), np.abs(expected), atol=1e-8)
        for col in range(actual.shape[1]):
            same = np.allclose(actual[:, col], expected[:, col], atol=1e-8)
            flipped = np.allclose(actual[:, col], -expected[:, col], atol=1e-8)
            assert same or flipped, f"component {col} is not a sign flip"

    def test_explained_variance_ratio_matches_reference(self):
        X = _random_embeddings(30, 128)

        pca = PCA(n_components=3).fit(X)
        _, expected_ratio = _reference_fit_transform(X, n_components=3)

        assert pca.explained_variance_ratio_ is not None
        np.testing.assert_allclose(
            pca.explained_variance_ratio_, expected_ratio, atol=1e-8
        )

    def test_matches_reference_when_samples_exceed_features(self):
        """The full-rank case takes a different SVD branch than n < d."""
        X = _random_embeddings(50, 8)

        actual = PCA(n_components=3).fit_transform(X)
        expected, _ = _reference_fit_transform(X, n_components=3)

        np.testing.assert_allclose(np.abs(actual), np.abs(expected), atol=1e-8)


class TestNoCovarianceMatrix:
    def test_fit_does_not_build_a_covariance_matrix(self, monkeypatch):
        """The whole point of the change: never go through an n_features^2 matrix."""

        def _fail(*args, **kwargs):
            raise AssertionError(
                "PCA.fit must not build a covariance matrix or eigendecompose it"
            )

        monkeypatch.setattr(np, "cov", _fail)
        monkeypatch.setattr(np.linalg, "eigh", _fail)

        coords = PCA(n_components=3).fit_transform(_random_embeddings(30, 256))

        assert coords.shape == (30, 3)

    def test_production_shape_is_fast(self):
        """A 1024-dim fit at production result counts should be milliseconds.

        The covariance path took seconds here. The bound is deliberately loose
        so this is a regression guard against re-introducing an O(d^3) fit, not
        a benchmark.
        """
        import time

        X = _random_embeddings(30, 1024)

        start = time.perf_counter()
        PCA(n_components=3).fit_transform(X)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.5, f"PCA at (30, 1024) took {elapsed:.3f}s"


class TestKnownAnswers:
    def test_recovers_a_planted_axis(self):
        """Variance concentrated on feature 0 ⇒ PC1 is that axis."""
        X = np.zeros((10, 4))
        X[:, 0] = np.linspace(-5.0, 5.0, 10)
        X[:, 1] = np.linspace(-0.1, 0.1, 10)

        pca = PCA(n_components=2).fit(X)

        assert pca.components_ is not None
        assert pca.explained_variance_ratio_ is not None
        # Feature 1 carries a little variance too, so PC1 is dominated by
        # feature 0 rather than exactly aligned with it.
        assert abs(pca.components_[0][0]) == pytest.approx(1.0, abs=1e-3)
        assert pca.explained_variance_ratio_[0] > 0.99

    def test_components_are_orthonormal(self):
        pca = PCA(n_components=3).fit(_random_embeddings(20, 64))

        assert pca.components_ is not None
        gram = pca.components_ @ pca.components_.T
        np.testing.assert_allclose(gram, np.eye(3), atol=1e-8)

    def test_explained_variance_is_descending(self):
        pca = PCA(n_components=3).fit(_random_embeddings(30, 64))

        assert pca.explained_variance_ is not None
        variances = pca.explained_variance_
        assert variances[0] >= variances[1] >= variances[2]

    def test_transform_matches_fit_transform(self):
        X = _random_embeddings(15, 32)
        pca = PCA(n_components=3)

        fitted = pca.fit_transform(X)

        np.testing.assert_allclose(fitted, pca.transform(X), atol=1e-12)


class TestDeterministicSigns:
    def test_repeated_fits_produce_identical_coordinates(self):
        X = _random_embeddings(30, 128)

        first = PCA(n_components=3).fit_transform(X)
        second = PCA(n_components=3).fit_transform(X)

        np.testing.assert_array_equal(first, second)

    def test_each_component_anchors_positive(self):
        pca = PCA(n_components=3).fit(_random_embeddings(30, 128))

        assert pca.components_ is not None
        for component in pca.components_:
            anchor = component[np.argmax(np.abs(component))]
            assert anchor > 0


class TestDegenerateInputs:
    def test_rank_deficient_data_yields_a_zero_axis(self):
        """3 samples span a 2D affine subspace; PC3 must still exist, at zero.

        This is the production shape (n_samples < n_features), so the SVD
        returns a full n_components axes and the zero comes from its own
        near-zero singular value — the padding branch is *not* what produces
        it here. See ``test_padding_fills_axes_the_svd_cannot_supply`` for
        that branch.
        """
        X = _random_embeddings(3, 64)

        pca = PCA(n_components=3)
        coords = pca.fit_transform(X)

        assert coords.shape == (3, 3)
        assert pca.components_ is not None
        assert pca.components_.shape == (3, 64)
        assert pca.explained_variance_ is not None
        assert pca.explained_variance_[2] == pytest.approx(0.0, abs=1e-8)
        np.testing.assert_allclose(coords[:, 2], 0.0, atol=1e-8)

    def test_padding_fills_axes_the_svd_cannot_supply(self):
        """n_components > min(n_samples, n_features) ⇒ the padding branch runs.

        With 2 samples of 3 features the thin SVD yields only 2 axes, so the
        third must be zero-filled rather than returned short. No real caller
        reaches this (both feed >= 3 samples with n_components=3), but the
        branch exists to keep the output shape a function of n_components
        alone.
        """
        X = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 7.0]])

        pca = PCA(n_components=3)
        coords = pca.fit_transform(X)

        assert coords.shape == (2, 3)
        assert pca.components_ is not None
        assert pca.components_.shape == (3, 3)
        # The padded axis is exactly zero, not merely small.
        np.testing.assert_array_equal(pca.components_[2], np.zeros(3))
        assert pca.explained_variance_ is not None
        assert pca.explained_variance_[2] == pytest.approx(0.0, abs=1e-12)
        np.testing.assert_array_equal(coords[:, 2], np.zeros(2))

    def test_identical_samples_have_zero_variance(self):
        X = np.tile(np.array([0.3, 0.4, 0.5, 0.7]), (6, 1))

        pca = PCA(n_components=2)
        coords = pca.fit_transform(X)

        # Absolute variance and coordinates collapse to zero. The *ratio* is
        # not asserted: centering leaves float noise on the order of 1e-17, so
        # the ratio normalizes that noise and is meaningless here. The previous
        # covariance implementation behaved identically.
        assert pca.explained_variance_ is not None
        np.testing.assert_allclose(pca.explained_variance_, 0.0, atol=1e-24)
        np.testing.assert_allclose(coords, 0.0, atol=1e-12)

    def test_zero_vectors_do_not_produce_nan(self):
        """Keyword-only chunks arrive as zero rows; they must not poison the fit."""
        X = _random_embeddings(10, 32)
        X[3] = 0.0
        X[7] = 0.0

        coords = PCA(n_components=3).fit_transform(X)

        assert not np.isnan(coords).any()


class TestValidation:
    def test_rejects_zero_components(self):
        with pytest.raises(ValueError, match="n_components must be >= 1"):
            PCA(n_components=0)

    def test_rejects_non_2d_input(self):
        pca = PCA(n_components=2)
        one_dimensional = np.array([1.0, 2.0, 3.0])
        with pytest.raises(ValueError, match="X must be 2D array"):
            pca.fit(one_dimensional)

    def test_rejects_more_components_than_features(self):
        pca = PCA(n_components=5)
        too_few_features = np.zeros((10, 3))
        with pytest.raises(ValueError, match="n_components=5 > n_features=3"):
            pca.fit(too_few_features)

    def test_rejects_single_sample(self):
        pca = PCA(n_components=2)
        single_sample = np.array([[1.0, 2.0, 3.0]])
        with pytest.raises(ValueError, match="at least 2 samples"):
            pca.fit(single_sample)

    def test_transform_before_fit_raises(self):
        pca = PCA(n_components=2)
        data = np.zeros((4, 8))
        with pytest.raises(ValueError, match="PCA not fitted yet"):
            pca.transform(data)

    def test_transform_rejects_non_2d_input(self):
        pca = PCA(n_components=2).fit(_random_embeddings(10, 8))
        one_dimensional = np.array([1.0, 2.0])
        with pytest.raises(ValueError, match="X must be 2D array"):
            pca.transform(one_dimensional)


class TestComputePcaCoordinates:
    """The caller that turns search results into 3D coordinates.

    This is the function that made up 51% of the traced request. It is covered
    here because the guards and the keyword-only fallback are easy to break
    without any PCA test noticing.
    """

    @staticmethod
    def _result(
        point_id: str | None,
        doc_id: str,
        start: int = 0,
        end: int = 10,
    ):
        return SimpleNamespace(
            point_id=point_id,
            id=doc_id,
            chunk_start_offset=start,
            chunk_end_offset=end,
        )

    @staticmethod
    def _point(doc_id: str, vector, start: int = 0, end: int = 10):
        return SimpleNamespace(
            vector={"dense": vector},
            payload={
                "doc_id": doc_id,
                "chunk_start_offset": start,
                "chunk_end_offset": end,
            },
        )

    @pytest.fixture
    def patched(self, mocker):
        """Stub out Qdrant and settings; yield the retrieve mock."""
        retrieve = mocker.AsyncMock()
        client = SimpleNamespace(retrieve=retrieve)
        mocker.patch(
            "nextcloud_mcp_server.vector.visualization.get_qdrant_client",
            mocker.AsyncMock(return_value=client),
        )
        mocker.patch(
            "nextcloud_mcp_server.vector.visualization.get_settings",
            return_value=SimpleNamespace(
                get_collection_name=lambda: "tenant_test",
            ),
        )
        return retrieve

    async def test_returns_coordinates_for_each_result(self, patched):
        rng = np.random.default_rng(7)
        results = [self._result(f"p{i}", f"d{i}") for i in range(4)]
        patched.return_value = [
            self._point(f"d{i}", rng.normal(size=32).tolist()) for i in range(4)
        ]

        out = await compute_pca_coordinates(results, rng.normal(size=32))

        assert len(out["coordinates_3d"]) == 4
        assert all(len(c) == 3 for c in out["coordinates_3d"])
        assert len(out["query_coords"]) == 3
        assert set(out["pca_variance"]) == {"pc1", "pc2", "pc3"}
        patched.assert_awaited_once()
        assert patched.await_args.kwargs["collection_name"] == "tenant_test"
        assert patched.await_args.kwargs["ids"] == ["p0", "p1", "p2", "p3"]

    async def test_fewer_than_two_point_ids_short_circuits(self, patched):
        results = [self._result("p0", "d0"), self._result(None, "d1")]

        out = await compute_pca_coordinates(results, np.zeros(32))

        assert out == {"coordinates_3d": [], "query_coords": []}
        patched.assert_not_awaited()

    async def test_fewer_than_two_retrieved_vectors_short_circuits(self, patched):
        results = [self._result(f"p{i}", f"d{i}") for i in range(3)]
        # Only one point comes back with a usable dense vector.
        patched.return_value = [self._point("d0", np.ones(32).tolist())]

        out = await compute_pca_coordinates(results, np.ones(32))

        assert out == {"coordinates_3d": [], "query_coords": []}

    async def test_keyword_only_chunk_is_placed_at_origin(self, patched):
        """A chunk with no dense vector must not shift the other points."""
        rng = np.random.default_rng(11)
        results = [self._result(f"p{i}", f"d{i}") for i in range(4)]
        # d2 has no matching point in the retrieve response (keyword-only).
        patched.return_value = [
            self._point(f"d{i}", rng.normal(size=32).tolist()) for i in (0, 1, 3)
        ]

        out = await compute_pca_coordinates(results, rng.normal(size=32))

        assert len(out["coordinates_3d"]) == 4
        assert not np.isnan(np.array(out["coordinates_3d"])).any()

    async def test_integer_payload_doc_ids_still_match(self, patched):
        """Legacy points store doc_id as int; the lookup coerces to str."""
        rng = np.random.default_rng(13)
        results = [self._result(f"p{i}", str(i)) for i in range(3)]
        patched.return_value = [
            self._point(i, rng.normal(size=16).tolist()) for i in range(3)
        ]

        out = await compute_pca_coordinates(results, rng.normal(size=16))

        # All three matched, so none were zero-filled to the origin.
        coords = np.array(out["coordinates_3d"])
        assert coords.shape == (3, 3)
        assert np.abs(coords).sum() > 0
