// Initialize vizApp for vector visualization
function vizApp() {
    return {
        query: '',
        algorithm: 'bm25_hybrid',
        fusion: 'rrf',
        showAdvanced: false,
        showQueryPoint: true,
        docTypes: [''],
        limit: 50,
        scoreThreshold: 0.0,
        loading: false,
        results: [],
        coordinates: null,
        queryCoords: null,
        expandedChunks: {},
        chunkLoading: {},

        async executeSearch() {
            this.loading = true;
            this.results = [];

            try {
                const params = new URLSearchParams({
                    query: this.query,
                    algorithm: this.algorithm,
                    limit: this.limit,
                    score_threshold: this.scoreThreshold,
                });

                if (this.algorithm === 'bm25_hybrid') {
                    params.append('fusion', this.fusion);
                }

                const selectedTypes = this.docTypes.filter(t => t !== '');
                if (selectedTypes.length > 0) {
                    params.append('doc_types', selectedTypes.join(','));
                }

                const response = await fetch(`/app/vector-viz/search?${params}`);
                const data = await response.json();

                if (data.success) {
                    this.results = data.results;
                    this.coordinates = data.coordinates_3d;
                    this.queryCoords = data.query_coords;
                    this.renderPlot(this.coordinates, this.queryCoords, this.results);
                } else {
                    alert('Search failed: ' + data.error);
                }
            } catch (error) {
                alert('Error: ' + error.message);
            } finally {
                this.loading = false;
            }
        },

        updatePlot() {
            // Re-render plot with current data when toggle changes
            if (this.coordinates && this.queryCoords && this.results.length > 0) {
                this.renderPlot(this.coordinates, this.queryCoords, this.results);
            }
        },

        renderPlot(coordinates, queryCoords, results) {
            const scores = results.map(r => r.score);

            // Trace 1: Document results
            const documentTrace = {
                x: coordinates.map(c => c[0]),
                y: coordinates.map(c => c[1]),
                z: coordinates.map(c => c[2]),
                mode: 'markers',
                type: 'scatter3d',
                name: 'Documents',
                customdata: results.map((r, i) => ({
                    title: r.title,
                    raw_score: r.original_score,
                    relative_score: r.score,
                    x: coordinates[i][0],
                    y: coordinates[i][1],
                    z: coordinates[i][2]
                })),
                hovertemplate:
                    '<b>%{customdata.title}</b><br>' +
                    'Raw Score: %{customdata.raw_score:.3f} (%{customdata.relative_score:.0%} relative)<br>' +
                    '(x=%{customdata.x}, y=%{customdata.y}, z=%{customdata.z})' +
                    '<extra></extra>',
                marker: {
                    size: results.map(r => 4 + (Math.pow(r.score, 2) * 10)),
                    opacity: results.map(r => 0.3 + (r.score * 0.7)),
                    color: scores,
                    colorscale: 'Viridis',
                    showscale: true,
                    colorbar: { title: 'Relative Score' },
                    cmin: 0,
                    cmax: 1
                }
            };

            // Trace 2: Query point (distinct marker)
            const queryTrace = {
                x: [queryCoords[0]],
                y: [queryCoords[1]],
                z: [queryCoords[2]],
                mode: 'markers',
                type: 'scatter3d',
                name: 'Query',
                hovertemplate:
                    '<b>Search Query</b><br>' +
                    `(x=${queryCoords[0]}, y=${queryCoords[1]}, z=${queryCoords[2]})` +
                    '<extra></extra>',
                marker: {
                    size: 10,
                    color: '#ef5350',  // Subdued red (Material Design Red 400)
                    line: {
                        color: '#c62828',  // Darker red border (Material Design Red 800)
                        width: 1
                    }
                }
            };

            // Preserve camera position if plot already exists
            const plotDiv = document.getElementById('viz-plot');
            let cameraSettings = { eye: { x: 1.5, y: 1.5, z: 1.5 } }; // Default camera position

            if (plotDiv && plotDiv.layout && plotDiv.layout.scene && plotDiv.layout.scene.camera) {
                // Plot exists and has been interacted with - preserve current camera
                cameraSettings = plotDiv.layout.scene.camera;
            }

            const layout = {
                title: `Vector Space (PCA 3D) - ${results.length} results`,
                scene: {
                    xaxis: { title: 'PC1' },
                    yaxis: { title: 'PC2' },
                    zaxis: { title: 'PC3' },
                    camera: cameraSettings
                },
                hovermode: 'closest',
                autosize: true,  // Enable auto-sizing to fit container
                showlegend: true,
                margin: { l: 0, r: 0, t: 40, b: 0 }  // Minimize margins for full width
            };

            // Conditionally include query trace based on toggle
            const traces = this.showQueryPoint ? [documentTrace, queryTrace] : [documentTrace];

            // Enable responsive resizing
            const config = {
                responsive: true,
                displayModeBar: true
            };

            // Use Plotly.react() instead of newPlot() to preserve camera position
            // when toggling query point visibility
            Plotly.react('viz-plot', traces, layout, config);
        },

        getNextcloudUrl(result) {
            // Use global NEXTCLOUD_BASE_URL if set, otherwise construct from window location
            const baseUrl = window.NEXTCLOUD_BASE_URL || '';
            switch (result.doc_type) {
                case 'note':
                    return `${baseUrl}/apps/notes/note/${result.id}`;
                case 'file':
                    return `${baseUrl}/apps/files/?fileId=${result.id}`;
                case 'calendar':
                    return `${baseUrl}/apps/calendar`;
                case 'contact':
                    return `${baseUrl}/apps/contacts`;
                case 'deck':
                    return `${baseUrl}/apps/deck`;
                default:
                    return `${baseUrl}`;
            }
        },

        hasChunkPosition(result) {
            return result.chunk_start_offset != null && result.chunk_end_offset != null;
        },

        isChunkExpanded(resultKey) {
            return this.expandedChunks[resultKey] !== undefined;
        },

        async toggleChunk(result) {
            const resultKey = `${result.doc_type}_${result.id}`;

            if (this.isChunkExpanded(resultKey)) {
                delete this.expandedChunks[resultKey];
                return;
            }

            this.chunkLoading[resultKey] = true;

            try {
                const params = new URLSearchParams({
                    doc_type: result.doc_type,
                    doc_id: result.id,
                    start: result.chunk_start_offset,
                    end: result.chunk_end_offset,
                    context: 500
                });

                const response = await fetch(`/app/chunk-context?${params}`);
                const data = await response.json();

                if (data.success) {
                    this.expandedChunks[resultKey] = data;
                } else {
                    alert('Failed to load chunk: ' + data.error);
                }
            } catch (error) {
                alert('Error loading chunk: ' + error.message);
            } finally {
                delete this.chunkLoading[resultKey];
            }
        }
    };
}
