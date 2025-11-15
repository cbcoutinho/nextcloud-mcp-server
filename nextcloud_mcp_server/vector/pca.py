"""Custom PCA implementation for dimensionality reduction.

Implements Principal Component Analysis without scikit-learn dependency.
Used for reducing high-dimensional embeddings (768-dim) to 2D for visualization.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


class PCA:
    """Principal Component Analysis for dimensionality reduction.

    Simple implementation that finds principal components via eigendecomposition
    of the covariance matrix. Suitable for small-to-medium datasets.

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
            ValueError: If X has fewer features than n_components
        """
        X = np.asarray(X)

        if X.ndim != 2:
            raise ValueError(f"X must be 2D array, got shape {X.shape}")

        n_samples, n_features = X.shape

        if n_features < self.n_components:
            raise ValueError(
                f"n_components={self.n_components} > n_features={n_features}"
            )

        # Center data
        self.mean_ = np.mean(X, axis=0)
        X_centered = X - self.mean_

        # Compute covariance matrix
        # Use (X^T X) / (n-1) for numerical stability with high-dim data
        cov = np.cov(X_centered.T)

        # Eigendecomposition
        eigenvalues, eigenvectors = np.linalg.eigh(cov)

        # Sort by eigenvalue (descending)
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]

        # Keep top n_components
        self.components_ = eigenvectors[:, : self.n_components].T
        self.explained_variance_ = eigenvalues[: self.n_components]

        # Calculate explained variance ratio
        total_variance = np.sum(eigenvalues)
        if total_variance > 0:
            self.explained_variance_ratio_ = self.explained_variance_ / total_variance
        else:
            self.explained_variance_ratio_ = np.zeros(self.n_components)

        logger.debug(
            f"PCA fit: {n_samples} samples, {n_features} features → "
            f"{self.n_components} components, "
            f"explained variance: {self.explained_variance_ratio_}"
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

        X = np.asarray(X)

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
