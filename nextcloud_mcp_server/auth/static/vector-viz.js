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

        init() {
            // Set up window resize listener to resize plot
            window.addEventListener('resize', () => {
                if (this.coordinates && this.results.length > 0) {
                    Plotly.Plots.resize('viz-plot');
                }
            });
        },

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
            // Toggle query point visibility without recreating the plot
            // This preserves camera position naturally since layout is untouched
            if (this.coordinates && this.queryCoords && this.results.length > 0) {
                const plotDiv = document.getElementById('viz-plot');

                // If plot exists, just toggle the query trace visibility
                if (plotDiv && plotDiv.data && plotDiv.data.length >= 2) {
                    // Trace index 1 is the query point
                    Plotly.restyle('viz-plot', { visible: this.showQueryPoint }, [1]);
                } else {
                    // Plot doesn't exist yet, render it
                    this.renderPlot(this.coordinates, this.queryCoords, this.results);
                }
            }
        },

        renderPlot(coordinates, queryCoords, results) {
            // Get container dimensions before creating layout
            const container = document.getElementById('viz-plot-container');
            const width = container.clientWidth;
            const height = container.clientHeight;

            const scores = results.map(r => r.score);

            // Trace 1: Document results (always visible)
            const documentTrace = {
                x: coordinates.map(c => c[0]),
                y: coordinates.map(c => c[1]),
                z: coordinates.map(c => c[2]),
                mode: 'markers',
                type: 'scatter3d',
                name: 'Documents',
                visible: true,
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
                    colorbar: {
                        title: 'Relative Score',
                        x: 1.02,
                        xanchor: 'left',
                        thickness: 20,
                        len: 0.8
                    },
                    cmin: 0,
                    cmax: 1
                }
            };

            // Trace 2: Query point (visibility controlled by toggle)
            const queryTrace = {
                x: [queryCoords[0]],
                y: [queryCoords[1]],
                z: [queryCoords[2]],
                mode: 'markers',
                type: 'scatter3d',
                name: 'Query',
                visible: this.showQueryPoint,  // Initial visibility from state
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

            const layout = {
                title: `Vector Space (PCA 3D) - ${results.length} results`,
                width: width,   // Explicit width from container
                height: height, // Explicit height from container
                scene: {
                    xaxis: { title: 'PC1' },
                    yaxis: { title: 'PC2' },
                    zaxis: { title: 'PC3' },
                    camera: {
                        eye: { x: 1.5, y: 1.5, z: 1.5 }
                    },
                    // Full width for 3D scene
                    domain: {
                        x: [0, 1],
                        y: [0, 1]
                    }
                },
                hovermode: 'closest',
                autosize: true,  // Enable auto-sizing for window resizes
                showlegend: false,  // Hide legend
                margin: { l: 0, r: 100, t: 40, b: 0 }  // Right margin for colorbar
            };

            // Always render both traces - visibility is controlled by the visible property
            const traces = [documentTrace, queryTrace];

            // Enable responsive resizing
            const config = {
                responsive: true,
                displayModeBar: true
            };

            // Use newPlot() with explicit dimensions - renders at correct size immediately
            // Camera position will be preserved by subsequent Plotly.restyle() calls in updatePlot()
            Plotly.newPlot('viz-plot', traces, layout, config);
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
                case 'news_item':
                    return `${baseUrl}/apps/news/item/${result.id}`;
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
            const resultKey = `${result.doc_type}_${result.id}_${result.chunk_start_offset || 0}`;

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
