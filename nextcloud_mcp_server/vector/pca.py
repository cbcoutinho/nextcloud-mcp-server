"""Custom PCA implementation for dimensionality reduction.

Implements Principal Component Analysis without scikit-learn dependency.
Used for reducing high-dimensional embeddings (768/1024-dim) to 2D/3D for
visualization.

Fitting goes through a thin SVD of the centered sample matrix rather than an
eigendecomposition of the feature covariance matrix. The two are mathematically
equivalent, but covariance costs O(n_features^3): at the shapes this module
actually sees (a few dozen embeddings of 1024 dims) that meant building a
1024x1024 matrix and eigendecomposing it to recover 3 components, which
measured at ~5.6s and was over half the latency of a hybrid search request.
The thin SVD is O(min(n, d)^2 * max(n, d)) instead, which for this module's
shapes (n_samples much smaller than n_features) means O(n_samples^2 *
n_features).
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def _flip_component_signs(components: np.ndarray) -> np.ndarray:
    """Fix the sign of each component deterministically.

    An SVD determines each axis only up to sign, so equivalent fits can mirror
    the visualization between requests. Anchor each component on its
    largest-magnitude entry and make that entry positive. All-zero components
    (padding for degenerate axes) are left untouched.

    Note this is not sklearn's ``svd_flip``, which anchors on the largest
    entry of the corresponding *left* singular vector instead. Both are
    deterministic; they can disagree on which sign a given axis gets, so don't
    expect coordinate-level parity with sklearn.

    Args:
        components: Array of shape (n_components, n_features)

    Returns:
        The same components with signs normalized.
    """
    anchors = np.argmax(np.abs(components), axis=1)
    anchor_values = components[np.arange(components.shape[0]), anchors]
    signs = np.where(anchor_values < 0, -1.0, 1.0)
    return components * signs[:, np.newaxis]


class PCA:
    """Principal Component Analysis for dimensionality reduction.

    Finds principal components via a thin SVD of the centered sample matrix.
    Component signs follow a deterministic convention (the largest-magnitude
    entry of each component is made positive), so repeated fits of the same
    data produce identical coordinates rather than arbitrarily mirrored axes.

    Attributes:
        n_components: Number of principal components to keep
        mean_: Mean of training data (set during fit)
        components_: Principal components (eigenvectors)
        explained_variance_: Variance explained by each component
        explained_variance_ratio_: Fraction of total variance explained
    """

    def __init__(self, n_components: int = 2):
        """Initialize PCA.

        Args:
            n_components: Number of components to keep (default: 2)
        """
        if n_components < 1:
            raise ValueError(f"n_components must be >= 1, got {n_components}")

        self.n_components = n_components
        self.mean_: np.ndarray | None = None
        self.components_: np.ndarray | None = None
        self.explained_variance_: np.ndarray | None = None
        self.explained_variance_ratio_: np.ndarray | None = None

    def fit(self, X: np.ndarray) -> "PCA":
        """Fit PCA model to data.

        Args:
            X: Training data of shape (n_samples, n_features)

        Returns:
            self (for method chaining)

        Raises:
            ValueError: If X is not 2D, has fewer features than n_components,
                or holds fewer than 2 samples (variance is undefined).
        """
        X = np.asarray(X, dtype=np.float64)

        if X.ndim != 2:
            raise ValueError(f"X must be 2D array, got shape {X.shape}")

        n_samples, n_features = X.shape

        if n_features < self.n_components:
            raise ValueError(
                f"n_components={self.n_components} > n_features={n_features}"
            )

        if n_samples < 2:
            raise ValueError(f"PCA needs at least 2 samples, got {n_samples}")

        # Center data
        self.mean_ = np.mean(X, axis=0)
        X_centered = X - self.mean_

        # Thin SVD of the centered samples. The right singular vectors are the
        # principal axes and the singular values give the variances directly, so
        # we never materialize the n_features x n_features covariance matrix.
        _u, singular_values, vt = np.linalg.svd(X_centered, full_matrices=False)

        # Variance along each axis. Matches the eigenvalues of the covariance
        # matrix, which uses the same (n_samples - 1) denominator.
        variances = (singular_values**2) / (n_samples - 1)

        # np.linalg.svd already returns singular values in descending order, so
        # the components are ordered by explained variance without a sort.
        components = vt[: self.n_components]
        explained_variance = variances[: self.n_components]

        # The SVD yields at most min(n_samples, n_features) axes. When more
        # components were requested than the data can span, pad with zero axes:
        # projecting onto them contributes a constant-zero coordinate, which is
        # what a degenerate axis should produce.
        missing = self.n_components - components.shape[0]
        if missing > 0:
            components = np.vstack([components, np.zeros((missing, n_features))])
            explained_variance = np.concatenate([explained_variance, np.zeros(missing)])

        self.components_ = _flip_component_signs(components)
        self.explained_variance_ = explained_variance

        # Calculate explained variance ratio against the total variance across
        # *all* axes, not just the retained ones.
        total_variance = np.sum(variances)
        if total_variance > 0:
            self.explained_variance_ratio_ = self.explained_variance_ / total_variance
        else:
            self.explained_variance_ratio_ = np.zeros(self.n_components)

        logger.debug(
            "PCA fit: %s samples, %s features → %s components, explained variance: %s",
            n_samples,
            n_features,
            self.n_components,
            self.explained_variance_ratio_,
        )

        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        """Transform data to principal component space.

        Args:
            X: Data to transform of shape (n_samples, n_features)

        Returns:
            Transformed data of shape (n_samples, n_components)

        Raises:
            ValueError: If PCA not fitted yet
        """
        if self.mean_ is None or self.components_ is None:
            raise ValueError("PCA not fitted yet. Call fit() first.")

        X = np.asarray(X, dtype=np.float64)

        if X.ndim != 2:
            raise ValueError(f"X must be 2D array, got shape {X.shape}")

        # Center using training mean
        X_centered = X - self.mean_

        # Project onto principal components
        X_transformed = np.dot(X_centered, self.components_.T)

        return X_transformed

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        """Fit PCA model and transform data in one step.

        Args:
            X: Training data of shape (n_samples, n_features)

        Returns:
            Transformed data of shape (n_samples, n_components)
        """
        self.fit(X)
        return self.transform(X)
