# ADR-001: Enhanced Note Search with Token-Based Relevance Ranking

## Status
Proposed

## Context
The current search implementation in the Nextcloud MCP server performs simple substring matching without relevance ranking. The existing method:
1. Fetches all notes
2. Performs case-insensitive substring matching on title and content
3. Returns matches without any ordering by relevance

This approach has several limitations:
- Requires exact substring matches
- No ranking by relevance
- Only finds notes where the exact query string appears
- Cannot prioritize more important matches (e.g., title vs content)
- Inefficient for large note collections

We need to improve the search functionality without adding external dependencies to enhance the user experience while maintaining simplicity.

## Decision
We will implement a token-based search with relevance ranking that:
1. Splits queries and note content into individual tokens (words)
2. Matches based on tokens rather than complete substrings
3. Applies weighted scoring with title matches valued higher than content matches
4. Sorts results by relevance score
5. Maintains backward compatibility with the existing API

## Implementation Details

### 1. Query Processing
The search query will be tokenized (split into individual words), normalized (converted to lowercase), and filtered for stop words if necessary:

```python
def process_query(query: str) -> list[str]:
    # Convert to lowercase and split into tokens
    tokens = query.lower().split()
    # Filter out very short tokens (optional)
    tokens = [token for token in tokens if len(token) > 1]
    # Could add stop word removal here
    return tokens
```

### 2. Note Content Processing
Each note's title and content will be processed in a similar way:

```python
def process_note_content(note: dict) -> tuple[list[str], list[str]]:
    # Process title
    title = note.get("title", "").lower()
    title_tokens = title.split()
    
    # Process content
    content = note.get("content", "").lower()
    content_tokens = content.split()
    
    return title_tokens, content_tokens
```

### 3. Scoring Algorithm
We'll implement a scoring function that:
- Assigns higher weight to title matches (e.g., 3x more important than content matches)
- Considers the percentage of query tokens that match
- Factors in the frequency of matches

```python
def calculate_score(query_tokens: list[str], title_tokens: list[str], content_tokens: list[str]) -> float:
    # Constants for weighting
    TITLE_WEIGHT = 3.0
    CONTENT_WEIGHT = 1.0
    
    score = 0.0
    
    # Count matches in title
    title_matches = sum(1 for qt in query_tokens if qt in title_tokens)
    if query_tokens:  # Avoid division by zero
        title_match_ratio = title_matches / len(query_tokens)
        score += TITLE_WEIGHT * title_match_ratio
    
    # Count matches in content
    content_matches = sum(1 for qt in query_tokens if qt in content_tokens)
    if query_tokens:  # Avoid division by zero
        content_match_ratio = content_matches / len(query_tokens)
        score += CONTENT_WEIGHT * content_match_ratio
    
    # If no tokens matched at all, return zero
    if title_matches == 0 and content_matches == 0:
        return 0.0
        
    return score
```

### 4. Enhanced Search Implementation

```python
def notes_search_notes(self, *, query: str):
    """
    Search notes using token-based matching with relevance ranking.
    Returns notes sorted by relevance score.
    """
    all_notes = self.notes_get_all()
    search_results = []
    
    # Process the query
    query_tokens = process_query(query)
    
    # If empty query after processing, return empty results
    if not query_tokens:
        return []
    
    # Process and score each note
    for note in all_notes:
        title_tokens, content_tokens = process_note_content(note)
        score = calculate_score(query_tokens, title_tokens, content_tokens)
        
        # Only include notes with a non-zero score
        if score > 0:
            search_results.append({
                "id": note.get("id"),
                "title": note.get("title"),
                "category": note.get("category"),
                "modified": note.get("modified"),
                "_score": score  # Include score for sorting (optional field)
            })
    
    # Sort by score in descending order
    search_results.sort(key=lambda x: x["_score"], reverse=True)
    
    # Remove score field before returning (optional)
    for result in search_results:
        if "_score" in result:
            del result["_score"]
    
    return search_results
```

### 5. Performance Considerations
- The enhanced search still retrieves all notes from the server, which could be inefficient for large collections
- Future improvements could include caching or building an in-memory index
- For very large note collections, consider adding pagination to the API

## Consequences

### Benefits
1. Better search results with matches on individual words instead of exact phrases
2. Relevant results appear first due to ranking
3. Title matches are prioritized, matching user expectations
4. No additional dependencies required
5. Maintains backward compatibility with existing API

### Limitations
1. Slightly increased complexity in the search implementation
2. Still requires fetching all notes for each search operation
3. No handling of typos or similar words (would require fuzzy matching)
4. No stemming/lemmatization to match word variations

### Future Potential Enhancements
1. Add support for phrase queries (exact matches)
2. Implement an in-memory index for faster repeated searches
3. Add basic natural language processing features (stemming, stop words)
4. Support for fuzzy matching to handle typos

## Alternatives Considered
1. Implementing a full-text search engine (e.g., integrating with Elasticsearch)
2. Using vector-based semantic search with embeddings
3. Adding external NLP libraries for more sophisticated text processing

These alternatives were not selected for the initial implementation due to the desire to maintain simplicity and avoid adding dependencies, but could be considered for future enhancements.
