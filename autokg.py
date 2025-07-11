import re
import sys
import numpy as np
import json
import matplotlib.pyplot as plt

from annoy import AnnoyIndex
from scipy import sparse
from scipy.sparse import diags, csr_matrix
from sklearn.cluster import KMeans
from sklearn.metrics import pairwise_distances, silhouette_score
from sklearn.neighbors import NearestNeighbors
import graphlearning as gl
from sentence_transformers import SentenceTransformer

from utils import *  # Assumes helper functions like get_completion, get_num_tokens, process_strings, remove_duplicates, etc.

# Instead of using langchain_openai's OpenAIEmbeddings, we create a simple wrapper using litellm.
from litellm import embedding, token_counter, get_max_tokens

EXTRACT_RELATIONSHIP_BATCH_SIZE = 35

def extract_json(response_str):
    """
    Attempts to extract a JSON substring from the response.
    Searches for text within triple backticks with an optional 'json' marker,
    or between the first '[' and the last ']'.
    """
    import re
    pattern = r"```json\s*(.*?)\s*```"
    match = re.search(pattern, response_str, re.DOTALL)
    if match:
        return match.group(1)
    else:
        start = response_str.find('[')
        end = response_str.rfind(']')
        if start != -1 and end != -1 and end > start:
            return response_str[start:end + 1]
    return response_str


class autoKG:
    def __init__(self, texts: list, source: list, embedding_model: str,
                 llm_model: str, embedding_api_key: str, llm_api_key: str,
                 main_topic: str, embed: bool = True, embedding_key2: str = "",
                 embedding_key3: str = "", llm_key2: str = "",
                 llm_key3: str = ""):

        self.texts = texts
        self.embedding_model = embedding_model
        self.embedding_api_key = embedding_api_key
        self.embedding_key2 = embedding_key2
        self.embedding_key3 = embedding_key3
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.llm_key2 = llm_key2
        self.llm_key3 = llm_key3
        # self.set_api_key_variables()
        self.source = source

        llm_status, embedding_status = set_env_variables(self.llm_model,
                                                         self.embedding_model,
                                                         [self.llm_api_key,
                                                          self.llm_key2,
                                                          self.llm_key3], [
                                                             self.embedding_api_key,
                                                             self.embedding_key2,
                                                             self.embedding_key3])
        self.check_component_status(llm_status, embedding_status)
        if determine_embedding_parent(self.embedding_model) == "local_embedding":
            if SentenceTransformer is None:
                print("sentence_transformers not installed; local embedding will fail")
                self.local_embedder = None
            else:
                try:
                    self.local_embedder = SentenceTransformer(self.embedding_model)
                    print(f"Loaded local embedder {self.embedding_model}")
                except Exception as e:
                    print(f"Failed to load local embedder {self.embedding_model}: {e}")
                    self.local_embedder = None
        else: self.local_embedder = None

        if embed:
            self.vectors = np.array(self.embed_documents(self.texts))
        else:
            self.vectors = None

        self.weightmatrix = None
        self.graph = None
        self.encoding = token_counter
        self.token_counts = get_num_tokens(texts,
                                           model=llm_model) if texts is not None else None

        self.separator = "\n* "
        self.main_topic = main_topic

        # For the keywords graph:
        self.keywords = None
        self.keyvectors = None
        self.U_mat = None
        self.pred_mat = None
        self.A = None
        self.dist_mat = None

        # Generation parameters (temperature set to 0 for determinism)
        self.temperature = 0.0
        self.top_p = 0.5

    def check_component_status(self, llm_status, embedding_status):
        failed = []
        if not llm_status:
            failed.append("LLM")
        if not embedding_status:
            failed.append("Embedding")
        if failed:
            print(f"The following model selections failed: {', '.join(failed)}")

    def embed_documents(self, texts: list):
        embeddings = []
        total = len(texts)


        # Decide embedding call once
        parent = determine_embedding_parent(self.embedding_model)
        use_ollama = (parent == "ollama_embedding")
        use_local = (parent == "local_embedding" and self.local_embedder is not None)

        for idx, text in enumerate(texts, 1):
            print(
                f"Embedding {idx}/{total}: {repr(text[:100])}...")  # show first 100 chars safely
            if use_local:
                vec = self.local_embedder.encode(text)
            else:
                if use_ollama:
                    emb = embedding(model=self.embedding_model, input=[text],
                                    api_base=self.embedding_api_key)
                else:
                    emb = embedding(model=self.embedding_model, input=text)
                vec = emb.data[0]['embedding']
            embeddings.append(vec)

        return embeddings

    def update_keywords(self, keyword_list):
        self.keywords = keyword_list
        self.keyvectors = np.array(
            self.embed_documents(self.keywords))

    def make_graph(self, k, method='annoy', similarity='angular',
                   kernel='gaussian'):
        knn_data = gl.weightmatrix.knnsearch(self.vectors, k, method,
                                             similarity)
        W = gl.weightmatrix.knn(None, k, kernel, symmetrize=True,
                                knn_data=knn_data)
        self.weightmatrix = W
        self.graph = gl.graph(W)

    def remove_same_text(self, use_nn=True, n_neighbors=5, thresh=1e-6,
                         update=True):
        to_delete = set()
        to_keep_set = set()
        if use_nn:
            nbrs = NearestNeighbors(n_neighbors=n_neighbors + 1,
                                    metric='cosine').fit(self.vectors)
            distances, indices = nbrs.kneighbors(self.vectors)
            for i in range(self.vectors.shape[0]):
                for j, distance in zip(indices[i], distances[i]):
                    if i != j and distance < thresh and i not in to_delete and j not in to_delete:
                        if self.token_counts[i] >= self.token_counts[j]:
                            to_delete.add(i)
                            if i in to_keep_set:
                                to_keep_set.remove(i)
                            if j not in to_delete:
                                to_keep_set.add(j)
                        else:
                            to_delete.add(j)
                            if j in to_keep_set:
                                to_keep_set.remove(j)
                            if i not in to_delete:
                                to_keep_set.add(i)
        else:
            D = pairwise_distances(self.vectors, metric='cosine')
            for i in range(self.vectors.shape[0]):
                for j in range(i + 1, self.vectors.shape[0]):
                    if D[i, j] < thresh:
                        if self.token_counts[i] >= self.token_counts[j]:
                            to_delete.add(i)
                            if i in to_keep_set:
                                to_keep_set.remove(i)
                            if j not in to_delete:
                                to_keep_set.add(j)
                        else:
                            to_delete.add(j)
                            if j in to_keep_set:
                                to_keep_set.remove(j)
                            if i not in to_delete:
                                to_keep_set.add(i)
        all_indices = set(range(self.vectors.shape[0]))
        to_keep = np.array(list(all_indices - to_delete)).astype(int)
        to_delete = np.array(list(to_delete)).astype(int)
        remains = np.array(list(to_keep_set))
        if update:
            self.texts = [self.texts[i] for i in to_keep]
            self.source = [self.source[i] for i in to_keep]
            self.vectors = self.vectors[to_keep]
            self.token_counts = [self.token_counts[i] for i in to_keep]
            self.weightmatrix = None
            self.graph = None
        return to_keep, to_delete, remains

    def core_text_filter(self, core_list, max_length):
        model = self.llm_model
        header = f"""
            You are an advanced AI assistant specialized in analyzing various pieces of information and providing precise summaries.
            In the following task, you will be provided with a list of keywords, each separated by a comma.
            You may split or reorganize the terms as needed.
            Rules:
            1. Remove duplicates or semantically similar terms.
            2. Each keyword must be at most {max_length} words.
            3. Do not include extra symbols.
            4. Do not add any prefix or label (such as "Processed Keywords:" or "Raw Keywords:") to your response."""
        if self.main_topic != "":
            header += f"5. Keywords must relate to the topic {self.main_topic}."
        examples = f"""
            Examples:
            Raw Keywords: Mineral processing EPC, Mineral processing, EPC Mineral processing.., circulation, suction, hollow shaft., alkali-acid purification
            Processed Keywords: Mineral processing EPC, circulation, suction, hollow shaft, alkali-acid purification
            """
        prompt = f"""
            {header}
            {examples}
            Raw Keywords: {",".join(core_list)}
            Processed Keywords:
            """
        input_tokens = self.encoding(model=self.llm_model,
                                     text=",".join(core_list))
        response, _, tokens = get_completion(prompt,
                                             model_name=model,
                                             max_tokens=input_tokens,
                                             temperature=self.temperature,
                                             top_p=self.top_p,
                                             llm_api_key=self.llm_api_key)
        print(f"Response for processing keywords:\n{response}")
        response = response[:-1] if response.endswith(".") else response
        response = response.strip()
        for prefix in ["Processed Keywords:", "Raw Keywords:"]:
            if response.startswith(prefix):
                response = response[len(prefix):].strip()
        process_keywords = response.split(',')
        process_keywords = [kw.strip() for kw in process_keywords if kw.strip()]
        return process_keywords, tokens

    def sub_entry_filter(self):
        if self.keywords is None:
            raise ValueError("Please extract keywords first.")
        strings = self.keywords.copy()
        i = 0
        while i < len(strings):
            for j in range(len(strings)):
                if i != j and strings[i] in strings[j]:
                    strings.pop(j)
                    if j < i:
                        i -= 1
                    break
            else:
                i += 1
        i = len(strings) - 1
        while i >= 0:
            for j in range(len(strings) - 1, -1, -1):
                if i != j and strings[i] in strings[j]:
                    strings.pop(j)
                    if j < i:
                        i -= 1
                    break
            else:
                i -= 1
        self.keywords = strings
        self.keyvectors = np.array(
            self.embed_documents(self.keywords))
        return strings

    def final_keywords_filter(self):
        if self.keywords is None:
            raise ValueError("Please extract keywords first.")
        header = """
            You have been provided a list of keywords, each separated by a comma.
            Your task is to process this list according to guidelines that refine its utility.
            Output the processed keywords separated by commas.
            """
        task1 = (
            "Concentration and Deduplication: Consolidate nearly identical keywords; retain only one instance.")
        task2 = (
            "Splitting: If a keyword comprises two distinct parts, split them and remove duplicates.")
        task3 = (
            "Deletion: Remove overly vague or broad keywords, e.g. 'things' or 'stuff'.")
        reminder = "Output only the processed keywords separated by commas. Each keyword should be at most 4 words."
        keyword_string = ",".join(self.keywords)

        def quick_prompt(keyword_string, task):
            return f"""
                {header}
                Input Keywords: {keyword_string}
                Instructions: {task}
                {reminder}
                Your processed keywords:
                """

        all_tokens = 0
        input_tokens = self.encoding(model=self.llm_model, text=keyword_string)
        keyword_string, _, tokens = get_completion(
            quick_prompt(keyword_string, task1),
            model_name=self.llm_model,
            max_tokens=input_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            llm_api_key=self.llm_api_key
        )
        all_tokens += tokens

        input_tokens = self.encoding(model=self.llm_model, text=keyword_string)
        keyword_string, _, tokens = get_completion(
            quick_prompt(keyword_string, task2),
            model_name=self.llm_model,
            max_tokens=input_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            llm_api_key=self.llm_api_key
        )
        all_tokens += tokens

        input_tokens = self.encoding(model=self.llm_model, text=keyword_string)
        keyword_string, _, tokens = get_completion(
            quick_prompt(keyword_string, task3),
            model_name=self.llm_model,
            max_tokens=input_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            llm_api_key=self.llm_api_key)
        all_tokens += tokens

        keywords_list = keyword_string.split(",")
        cleaned_keywords = []
        for kw in keywords_list:
            kw = kw.strip()
            if kw.lower().startswith("processed keywords:"):
                kw = kw[len("processed keywords:"):].strip()
            if kw.lower().startswith("raw keywords:"):
                kw = kw[len("raw keywords:"):].strip()
            cleaned_keywords.append(kw)
        self.keywords = cleaned_keywords
        self.keyvectors = np.array(
            self.embed_documents(self.keywords))
        return ",".join(self.keywords), all_tokens

    def summary_contents(self, indx, sort_inds, avoid_content=None,
                         max_texts=5, prompt_language='English',
                         num_topics=10, max_length=4, show_prompt=False):
        if avoid_content is None:
            avoid_content = []
        model = self.llm_model
        max_num_tokens = get_max_tokens(model=model)
        header = f"""
            You are an advanced AI assistant specialized in summarizing information.
            Determine the core theme from the following fragments (delimited by triple backticks).
            Your answer must be a comma-separated list of at most {num_topics} keywords, each at most {max_length} words.
            Write your answer in {prompt_language} without full sentences.
            These keywords have already been chosen so do not repeat them: <{",".join(avoid_content)}>
            """
        chosen_texts = []
        chosen_texts_len = self.encoding(model=self.llm_model, text=header)
        chosen_texts_len += self.encoding(model=self.llm_model,
                                          text=",".join(avoid_content))
        separator_len = self.encoding(model=self.llm_model, text=self.separator)
        for i in range(min(max_texts, len(sort_inds))):
            select_text = [self.texts[j] for j in indx][sort_inds[i]]
            num_tokens = self.encoding(model=self.llm_model, text=select_text)
            chosen_texts_len += num_tokens + separator_len
            if chosen_texts_len > max_num_tokens - 100:
                break
            chosen_texts.append(self.separator + select_text)
        prompt = f"""
            {header}
            Information: '''{''.join(chosen_texts)}'''
            Your response:
            """
        if show_prompt:
            print(prompt)
        response, _, tokens = get_completion(prompt,
                                             model_name=model,
                                             max_tokens=200,
                                             temperature=self.temperature,
                                             top_p=self.top_p)
        return response, tokens

    def _choose_n_clusters(self,
                           target_size: int = 8,
                           min_k: int = 2,
                           max_offset: int = 2) -> int:
        """
        Heuristic + local silhouette search to pick n_clusters.
        """
        N = len(self.vectors)
        if N < min_k:
            raise ValueError(
                "Text is too short to generate a knowledge graph. Please try to have more than 100 words.")

        # 1) initial guess by target size
        k0 = max(min_k, round(N / target_size))

        # 2) search in [k0 - max_offset, k0 + max_offset]
        best_k, best_score = k0, -1
        for k in range(max(min_k, k0 - max_offset), k0 + max_offset + 1):
            if k >= N:  # can't have more clusters than points
                continue
            labels = KMeans(n_clusters=k, n_init=5).fit_predict(self.vectors)
            score = silhouette_score(self.vectors, labels)
            if score > best_score:
                best_score, best_k = score, k

        return best_k

    def determine_k(self):
        if len(self.vectors) < 100:
            k = min(len(self.vectors) - 1,
                    max(round(len(self.vectors) / 10), 5))
        elif len(self.vectors) <= 500:
            k = 8 + round(len(self.vectors) / 50)
        else:
            k = min(50, round(len(self.vectors) / 25))
        return k

    def cluster(self, n_clusters: int = None, clustering_method='k_means',
                    max_texts=5, select_mtd='similarity',
                    prompt_language='English', num_topics=3, max_length=3,
                    post_process=True, add_keywords=True, verbose=False):

        if n_clusters is None:
            n_clusters = self._choose_n_clusters(target_size = 8, min_k = 2, max_offset = 2)

        if clustering_method == 'k_means':
            kmeans_model = KMeans(n_clusters, init='k-means++', n_init=10)
            kmeans = kmeans_model.fit(np.array(self.vectors))
        elif clustering_method in ['combinatorial', 'ShiMalik',
                                   'NgJordanWeiss']:
            extra_dim = 5
            if self.weightmatrix is None:
                k = self.determine_k()
                self.make_graph(k=k)
            n = self.graph.num_nodes
            if clustering_method == 'combinatorial':
                vals, vec = self.graph.eigen_decomp(k=n_clusters + extra_dim)
            elif clustering_method == 'ShiMalik':
                vals, vec = self.graph.eigen_decomp(normalization='randomwalk',
                                                    k=n_clusters + extra_dim)
            elif clustering_method == 'NgJordanWeiss':
                vals, vec = self.graph.eigen_decomp(normalization='normalized',
                                                    k=n_clusters + extra_dim)
                norms = np.sum(vec * vec, axis=1)
                T = sparse.spdiags(norms ** (-1 / 2), 0, n, n)
                vec = T @ vec
            kmeans = KMeans(n_clusters, init='k-means++', n_init=5).fit(vec)
        else:
            raise ValueError("Invalid clustering method.")
        all_tokens = 0
        cluster_names = []
        for i in range(len(kmeans.cluster_centers_)):
            center = kmeans.cluster_centers_[i]
            indx = np.arange(len(self.texts))[kmeans.labels_ == i]
            if select_mtd == 'similarity':
                if clustering_method == 'k_means':
                    sim_vals = pairwise_distances(self.vectors[indx],
                                                  center[np.newaxis, :],
                                                  metric='euclidean').flatten()
                else:
                    sim_vals = pairwise_distances(vec[indx],
                                                  center[np.newaxis, :],
                                                  metric='euclidean').flatten()
                sort_inds = np.argsort(sim_vals)
            elif select_mtd == 'random':
                sort_inds = np.random.permutation(len(indx))
            show_prompt = verbose and (i % 10 == 0)
            summary_center, tokens = self.summary_contents(indx=indx,
                                                           sort_inds=sort_inds,
                                                           avoid_content=cluster_names,
                                                           max_texts=max_texts,
                                                           prompt_language=prompt_language,
                                                           num_topics=num_topics,
                                                           max_length=max_length,
                                                           show_prompt=show_prompt)
            all_tokens += tokens
            processed_center, tokens = self.core_text_filter([summary_center],
                                                             max_length)
            all_tokens += tokens
            cluster_names.extend(processed_center)
            cluster_names = process_strings(cluster_names)
        if post_process:
            cluster_names, tokens = self.core_text_filter(cluster_names,
                                                          max_length)
            all_tokens += tokens
        cluster_names = list(set(cluster_names))
        output_keywords = list(set(self.keywords or []) | set(
            cluster_names)) if add_keywords else cluster_names
        self.keywords = process_strings(output_keywords)
        self.keyvectors = np.array(
            self.embed_documents(self.keywords))
        return cluster_names, all_tokens

    def distance_core_seg(self, core_texts, core_labels=None, k=20,
                          dist_metric='cosine', method='annoy',
                          return_full=False, return_prob=False):
        core_ebds = np.array(self.embed_documents(core_texts))
        if core_labels is None:
            core_labels = np.arange(len(core_ebds))
        else:
            core_labels = np.array(core_labels)
        k = min(k, len(core_texts))
        if method == 'annoy':
            similarity = 'angular' if dist_metric == 'cosine' else dist_metric
            knn_ind, knn_dist = autoKG.ANN_search(self.vectors, core_ebds, k,
                                                  similarity=similarity)
        elif method == 'dense':
            dist_mat = pairwise_distances(self.vectors, core_ebds,
                                          metric=dist_metric)
            knn_ind, knn_dist = [], []
            for i in range(len(dist_mat)):
                indices = np.argsort(dist_mat[i])[:k]
                knn_ind.append(indices)
                knn_dist.append(dist_mat[i][indices])
            knn_ind = np.array(knn_ind)
            knn_dist = np.arccos(1 - np.array(knn_dist))
        else:
            sys.exit("Invalid choice of method " + dist_metric)
        knn_ind = autoKG.replace_labels(knn_ind, core_labels)
        if return_prob:
            D = knn_dist * knn_dist
            eps = D[:, k - 1]
            weights = np.exp(-4 * D / eps[:, None])
            prob = weights / np.sum(weights, axis=1)[:, None]
        if return_full:
            return (knn_ind, knn_dist, prob) if return_prob else (
                knn_ind, knn_dist)
        else:
            return (
                knn_ind[:, 0], knn_dist[:, 0], prob[:, 0]) if return_prob else (
                knn_ind[:, 0], knn_dist[:, 0])

    def laplace_diffusion(self, core_texts, trust_num=10, core_labels=None,
                          dist_metric='cosine', return_full=False):
        if self.weightmatrix is None:
            k = self.determine_k()
            self.make_graph(k=k)
        knn_ind, knn_dist = self.distance_core_seg(core_texts, core_labels, k,
                                                   dist_metric, method='annoy',
                                                   return_full=False,
                                                   return_prob=False)
        if core_labels is None:
            core_labels = np.arange(len(core_texts))
        else:
            core_labels = np.array(core_labels)
        select_inds = np.array([], dtype=np.int64)
        select_labels = np.array([], dtype=np.int64)
        all_inds = np.arange(len(self.vectors))
        for i in range(len(core_texts)):
            select_ind = all_inds[knn_ind == i][
                np.argsort(knn_dist[knn_ind == i])[:trust_num]]
            select_inds = np.concatenate((select_inds, select_ind))
            select_labels = np.concatenate(
                (select_labels, core_labels[i] * np.ones(len(select_ind))))
        model = gl.ssl.laplace(self.weightmatrix)
        U = model._fit(select_inds, select_labels)
        if return_full:
            return U
        else:
            return np.argmax(U, axis=1)

    def PosNNeg_seg(self,
                    core_text,
                    trust_num: int = 5,
                    dist_metric: str = 'cosine',
                    negative_multiplier: int = 3,
                    seg_mtd: str = 'laplace'):
        """
        1) Finds the k nearest neighbors to core_text in self.vectors.
        2) Picks up to `trust_num` closest (positives) and
           up to `trust_num * negative_multiplier` farthest (negatives).
        3) Labels them 0 (positive) or 1 (negative).
        4) Runs either k-means on those subtexts, or laplace/poisson diffusion.
        Returns: (label_pred, U) where
          - label_pred[i] ∈ {0,1}
          - U[i] is the “score” for the positive class.
        """
        # 1) ensure your graph exists
        k = self.determine_k()
        if self.weightmatrix is None:
            self.make_graph(k=k)

        # 2) get distances: shape (1, N)
        knn_ind, knn_dist = self.distance_core_seg(
            [core_text], [0], k,
            dist_metric, method='dense',
            return_full=False, return_prob=False
        )
        # flatten to a 1D array of indices
        sort_ind = np.argsort(knn_dist)

        N = len(sort_ind)
        # 3) figure out how many positives/negatives we can actually take
        max_pos = min(trust_num, N)
        max_neg = min(negative_multiplier * trust_num, N - max_pos)

        # 4) pick them
        pos_inds = sort_ind[:max_pos]
        neg_inds = sort_ind[-max_neg:] if max_neg > 0 else np.array([],
                                                                      dtype=int)
        select_inds = np.concatenate([pos_inds, neg_inds])

        # 5) build labels array to match
        select_labels = np.concatenate([
            np.zeros(len(pos_inds), dtype=int),
            np.ones(len(neg_inds), dtype=int)
        ])

        # 6) segmentation
        if seg_mtd == 'kmeans':
            sub_texts = [self.texts[i] for i in select_inds]
            label_pred, U = self.distance_core_seg(
                sub_texts, select_labels, k,
                dist_metric, method='dense',
                return_full=False, return_prob=False
            )
            # turn distances into a positive‐class score
            U = np.exp(-U / np.max(U, axis=0))

        elif seg_mtd == 'laplace':
            model = gl.ssl.laplace(self.weightmatrix)
            # partial fit: returns shape (N, 2) if you passed 2 labels
            U_partial = model._fit(select_inds, select_labels)
            label_pred = np.argmax(U_partial, axis=1)
            U = U_partial[:, 0]  # score for class 0

        elif seg_mtd == 'poisson':
            model = gl.ssl.poisson(self.weightmatrix)
            U_partial = model._fit(select_inds, select_labels)
            label_pred = np.argmax(U_partial, axis=1)
            U = U_partial[:, 0]

        else:
            raise ValueError(f"Unknown seg_mtd: {seg_mtd}")

        return label_pred, U

    def coretexts_seg_individual(self, trust_num=5, core_labels=None,
                                 dist_metric='cosine', negative_multiplier=3,
                                 seg_mtd='laplace',
                                 return_mat=True, connect_threshold=1):
        if (self.weightmatrix is None or self.weightmatrix.shape[0] != len(self.texts)):
            k = self.determine_k()
            self.make_graph(k=k)
        core_texts = self.keywords
        if core_labels is None:
            core_labels = np.arange(len(core_texts))
        else:
            core_labels = np.array(core_labels)
        N_labels = np.max(core_labels) + 1
        U_mat = np.zeros((len(self.texts), len(core_labels)))
        pred_mat = np.zeros((len(self.texts), N_labels))
        for core_ind, core_text in enumerate(core_texts):
            label_pred, U = self.PosNNeg_seg(core_text, trust_num,
                                             dist_metric, negative_multiplier,
                                             seg_mtd)
            U_mat[:, core_ind] = U
            pred_mat[label_pred == 0, core_labels[core_ind]] = 1
        if connect_threshold < 1:
            num_conn = np.sum(pred_mat, axis=0)
            N = len(self.texts)
            large_inds = np.where(num_conn > N * connect_threshold)[0]
            num_elements = int(N * connect_threshold)
            for l_ind in large_inds:
                threshold = np.partition(U_mat[:, l_ind], -num_elements)[
                    -num_elements]
                pred_mat[:, l_ind] = np.where(U_mat[:, l_ind] >= threshold, 1,
                                              0)
        if return_mat:
            A = csr_matrix((pred_mat.T @ pred_mat).astype(int))
            A = A - diags(A.diagonal())
            self.U_mat = U_mat
            self.pred_mat = pred_mat
            self.A = A
            return pred_mat, U_mat, A
        else:
            self.U_mat = U_mat
            self.pred_mat = pred_mat
            return pred_mat, U_mat

    def apply_dynamic_threshold(self, percentile_threshold=50):
        """
        Keep only the top X percent of edges by score.
        Modifies self.A in place and returns the threshold used.
        """
        if self.A.nnz == 0:
            raise ValueError("Adjacency matrix has no nonzero entries.")

        # compute cutoff
        thr = np.percentile(self.A.data, 100 - percentile_threshold)

        # zero out everything below cutoff
        self.A.data[self.A.data < thr] = 0.0

        # remove those zeros from the CSR structure
        self.A.eliminate_zeros()

        return thr

    def content_check(self, include_keygraph=True, auto_embedding=False):
        is_valid = True
        for attr in ['keywords', 'keyvectors', 'U_mat', 'pred_mat', 'A',
                     'dist_mat',
                     'texts', 'vectors', 'token_counts', 'source']:
            if getattr(self, attr, None) is None:
                print(f'Please set up {attr} first.')
                is_valid = False
        return is_valid

    def get_dist_mat(self):
        if not self.content_check(include_keygraph=True):
            raise ValueError('Missing Contents')
        self.dist_mat = np.arccos(
            1 - pairwise_distances(self.keyvectors, self.vectors,
                                   metric='cosine'))

    def angular_search(self, query, k=5, search_mtd='pair_dist',
                       search_with='texts'):
        if not self.content_check(include_keygraph=False):
            raise ValueError('Missing Contents')
        if isinstance(query, str):
            query_vec = np.array(self.embed_documents([query]))
        elif isinstance(query, list):
            query_vec = np.array(self.embed_documents(query))
        else:
            raise ValueError("Query must be a string or list.")
        s_vecs = self.vectors if search_with == 'texts' else self.keyvectors
        if search_mtd == 'pair_dist':
            dist_mat = np.arccos(
                1 - pairwise_distances(query_vec, s_vecs, metric='cosine'))
            knn_ind = np.array(
                [np.argsort(dist_mat[i])[:k] for i in range(len(query_vec))])
            knn_dist = np.array([[dist_mat[i][j] for j in ind] for i, ind in
                                 enumerate(knn_ind)])
        elif search_mtd == 'knn':
            knn_ind, knn_dist = autoKG.ANN_search(query_vec, s_vecs, k,
                                                  similarity='angular')
        else:
            sys.exit('Invalid search method.')
        return knn_ind.astype(int), knn_dist

    def keyword_related_text(self, keyind, k, use_u=True):
        if not self.content_check(include_keygraph=True):
            raise ValueError('Missing Contents')
        if use_u:
            text_ind = np.argsort(self.U_mat[:, keyind])[::-1][:k].astype(
                int).tolist()
        else:
            if self.dist_mat is None:
                raise ValueError("dist_mat is None")
            text_ind = np.argsort(self.dist_mat[keyind, :])[:k].astype(
                int).tolist()
        return text_ind

    def top_k_indices_sparse(self, row_index, k):
        row = self.A.getrow(row_index)
        non_zero_indices = row.nonzero()[1]
        if non_zero_indices.size < k:
            return non_zero_indices
        non_zero_values = np.array(row.data)
        top_k_indices = non_zero_indices[
            np.argpartition(non_zero_values, -k)[-k:]]
        return top_k_indices.astype(int).tolist()

    def KG_prompt(self, query, search_nums=(10, 5, 2, 3, 1),
                  search_mtd='pair_dist', use_u=False):
        if not self.content_check(include_keygraph=True):
            raise ValueError('Missing Contents')
        text_ind, keyword_ind, adj_keyword_ind = [], [], []
        sim_text_ind, _ = self.angular_search(query, k=search_nums[0],
                                              search_mtd=search_mtd,
                                              search_with='texts')
        text_ind.extend([(i, -1) for i in sim_text_ind.tolist()[0]])
        sim_keyword_ind, _ = self.angular_search(query, k=search_nums[1],
                                                 search_mtd=search_mtd,
                                                 search_with='keywords')
        keyword_ind.extend(sim_keyword_ind.tolist()[0])
        for k_ind in sim_keyword_ind.tolist()[0]:
            t_ind = self.keyword_related_text(k_ind, k=search_nums[2],
                                              use_u=use_u)
            text_ind.extend([(i, k_ind) for i in t_ind])
            adj_text_inds = self.top_k_indices_sparse(k_ind, k=search_nums[3])
            adj_keyword_ind.extend([(i, k_ind) for i in adj_text_inds])
        adj_keyword_ind = remove_duplicates(adj_keyword_ind)
        adj_keyword_ind = [item for item in adj_keyword_ind if
                           item[0] not in keyword_ind]
        for k_ind, _ in adj_keyword_ind:
            t_ind = self.keyword_related_text(k_ind, k=search_nums[4],
                                              use_u=use_u)
            text_ind.extend([(i, k_ind) for i in t_ind])
        text_ind = remove_duplicates(text_ind)
        record = {'query': query, 'text': text_ind,
                  'sim_keywords': keyword_ind, 'adj_keywords': adj_keyword_ind}
        return record

    def completion_from_record(self, record, output_tokens=1024,
                               prompt_language='English', show_prompt=False,
                               prompt_keywords=True, include_source=False):
        model = self.llm_model
        max_num_tokens = get_max_tokens(model=model)
        if prompt_keywords:
            header_part = (
                "You will be given a set of keywords directly related to a query as well as adjacent keywords from the knowledge graph. "
                "Keywords will be separated by semicolons (;). "
                "Relevant texts will be provided within triple backticks."
            )
        else:
            header_part = "Relevant texts will be provided within triple backticks."
        header = f"""
            I want you to use the following information from a knowledge graph to address a query.
            {header_part}
            Please do not invent any information. Stick strictly to the facts provided.
            Your response must be written in {prompt_language}.
            """
        max_content_token = max_num_tokens - output_tokens - 150
        query = record['query']
        text_ind = record['text']
        keyword_ind = record['sim_keywords']
        adj_keyword_ind = record['adj_keywords']
        keywords_info = (
                "Keywords directly related to the query:\n" +
                "; ".join([f"{self.keywords[i]}" for i in keyword_ind]) +
                "\nAdjacent keywords according to the knowledge graph:\n" +
                "; ".join([f"{self.keywords[i]}" for i, _ in adj_keyword_ind])
        )
        chosen_texts = []
        chosen_texts_len = self.encoding(model=model, text=header + query)
        if prompt_keywords:
            chosen_texts_len += self.encoding(model=model, text=keywords_info)
        separator_len = self.encoding(model=model, text=self.separator)
        for t_ind, _ in text_ind:
            select_text = self.texts[t_ind]
            if include_source:
                select_source = self.source[t_ind]
                select_text = f"Source:{select_source} Content:{select_text}"
            num_tokens = self.encoding(model=model, text=select_text)
            chosen_texts_len += num_tokens + separator_len
            if chosen_texts_len > max_content_token:
                break
            chosen_texts.append(self.separator + select_text)
        ref_info = "Selected reference texts:\n" + ''.join(chosen_texts)
        if prompt_keywords:
            prompt = f"""
                {header}
                {keywords_info}
                Texts:
                '''{''.join(chosen_texts)}'''
                Your task:
                {query}
                Your response:
                """
        else:
            prompt = f"""
                {header}
                Texts:
                '''{''.join(chosen_texts)}'''
                Your task:
                {query}
                Your response:
                """
        if show_prompt:
            print(prompt)
        response, _, all_tokens = get_completion(prompt,
                                                 model_name=model,
                                                 max_tokens=output_tokens,
                                                 temperature=self.temperature,
                                                 top_p=self.top_p)
        return response, keywords_info, ref_info, all_tokens

    def draw_graph_from_record(self, record, node_colors=(
            [0, 1, 1], [0, 1, 0.5], [1, 0.7, 0.75]),
                               node_shape='o', edge_color='black',
                               edge_widths=(2, 0.5),
                               node_sizes=(500, 150, 50), font_color='black',
                               font_size=6, show_text=True, save_fig=False,
                               save_path='Subgraph_vis.png'):
        T = record['text']
        K1 = record['sim_keywords']
        K2 = record['adj_keywords']
        N = [element.replace(" ", "\n") for element in self.keywords]
        Q = 'Query'
        import networkx as nx
        G = nx.Graph()
        G.add_node(Q)
        for i in K1:
            G.add_edge(Q, N[i])
        for i in K1:
            for j in K1:
                if self.A[i, j] > 0:
                    G.add_edge(N[i], N[j])
            for k, _ in K2:
                if self.A[i, k] > 0:
                    G.add_edge(N[i], N[k])
        if show_text:
            for i, L in T:
                new_node = f"Text {i}"
                G.add_node(new_node)
                for j in L:
                    if j == -1:
                        G.add_edge(new_node, Q)
                    else:
                        G.add_edge(new_node, N[j])
        color_map = {Q: node_colors[0]}
        node_size_map = {node: node_sizes[0] for node in G.nodes}
        for node in N:
            color_map[node] = node_colors[1]
            node_size_map[node] = node_sizes[1]
        if show_text:
            for i, _ in T:
                color_map[f"Text {i}"] = node_colors[2]
                node_size_map[f"Text {i}"] = node_sizes[2]
        edge_width_map = {edge: edge_widths[0] for edge in G.edges}
        if show_text:
            for i, L in T:
                new_node = f"Text {i}"
                for j in L:
                    if j == -1:
                        edge_width_map[(new_node, Q)] = edge_widths[1]
                        edge_width_map[(Q, new_node)] = edge_widths[1]
                    else:
                        edge_width_map[(new_node, N[j])] = edge_widths[1]
                        edge_width_map[(N[j], new_node)] = edge_widths[1]
        pos = nx.spring_layout(G, seed=42, k=0.15, iterations=50, scale=2.0)
        nx.draw_networkx_edges(G, pos, alpha=0.4, edge_color=edge_color,
                               width=[edge_width_map[edge] for edge in G.edges])
        nx.draw_networkx_nodes(G, pos,
                               node_color=[color_map[node] for node in G.nodes],
                               node_size=[node_size_map[node] for node in
                                          G.nodes],
                               node_shape=node_shape)
        nx.draw_networkx_labels(G, pos, labels={node: node for node in G.nodes},
                                font_size=font_size, font_color=font_color)
        if save_fig:
            plt.tight_layout()
            plt.savefig(save_path, dpi=300)
        plt.figure()

    def save_data(self, save_path, include_texts=False):
        if include_texts:
            keywords_dic = {
                'keywords': self.keywords, 'keyvectors': self.keyvectors,
                'U_mat': self.U_mat, 'pred_mat': self.pred_mat, 'A': self.A,
                'texts': self.texts, 'embedding_vectors': self.vectors,
                'dist_mat': self.dist_mat, 'token_counts': self.token_counts,
                'source': self.source
            }
        else:
            keywords_dic = {
                'keywords': self.keywords, 'keyvectors': self.keyvectors,
                'U_mat': self.U_mat, 'pred_mat': self.pred_mat, 'A': self.A,
                'dist_mat': self.dist_mat
            }
        np.save(save_path, keywords_dic)
        print(f"Successfully saved to {save_path}")

    def load_data(self, load_path, include_texts=False):
        keywords_dic = np.load(load_path, allow_pickle=True).item()
        self.keywords = keywords_dic.get('keywords')
        self.keyvectors = keywords_dic.get('keyvectors')
        self.U_mat = keywords_dic.get('U_mat')
        self.pred_mat = keywords_dic.get('pred_mat')
        self.A = keywords_dic.get('A')
        self.dist_mat = keywords_dic.get('dist_mat')
        if include_texts:
            if "texts" in keywords_dic:
                self.texts = keywords_dic.get('texts')
                self.vectors = keywords_dic.get('embedding_vectors')
                self.token_counts = keywords_dic.get('token_counts')
                self.source = keywords_dic.get('source')
            else:
                print("Failed to load texts information.")
        print(f"Successfully loaded from {load_path}")

    def write_keywords(self, save_path):
        if not self.content_check(include_keygraph=False):
            raise ValueError('Missing Contents')
        result = ''
        for i in range(len(self.keywords)):
            result += self.keywords[i]
            result += '\n' if (i + 1) % 10 == 0 else '; '
        with open(save_path, 'w') as f:
            f.write(result)

    def unify_directional_relationships(self, pair_edges):
        """
        Unifies known inverse relationship phrases into a canonical edge.
        Each edge is a tuple (entity1, relationship, entity2, direction).
        """
        unique_edges = []
        for e in pair_edges:
            if e not in unique_edges:
                unique_edges.append(e)
        inverse_map = {
            "employs": "employed by",
            "manages": "managed by",
            "father of": "child of",
            # Extend mapping as needed.
        }
        final_edges = []
        used = set()
        for (sub, rel, obj, direction) in unique_edges:
            if (sub, rel, obj, direction) in used:
                continue
            inv = inverse_map.get(rel)
            if inv and (sub, inv, obj, direction) in unique_edges:
                chosen = (sub, rel, obj, direction)
                final_edges.append(chosen)
                used.add(chosen)
                used.add((sub, inv, obj, direction))
            else:
                final_edges.append((sub, rel, obj, direction))
                used.add((sub, rel, obj, direction))
        return final_edges

    def chunk_transcript_sliding(self, transcript: str, safety_margin=600,
                                 overlap_ratio=0.1):
        """
        Splits the transcript into overlapping chunks using dynamic token limits.

        The maximum tokens per chunk is determined by get_max_tokens(self.llm_model) minus a safety margin.
        Instead of adding one word at a time, this function estimates how many words roughly correspond to
        10% of the maximum token allowance and then increases by that estimated amount per iteration.
        Overlap is computed as a fraction (overlap_ratio) of the chunk token limit.

        Uses self.encoding(model=self.llm_model, text=<text>) to get the token count.
        """
        max_tokens_allowed = get_max_tokens(model=self.llm_model)
        chunk_token_limit = max_tokens_allowed - safety_margin
        words = transcript.split()
        chunks = []
        current_chunk_words = []
        i = 0

        # Estimate how many words roughly correspond to 10% of the max token limit.
        # Use a small sample of words (or all if transcript is short).
        sample_size = 20 if len(words) >= 20 else len(words)
        sample_text = " ".join(words[:sample_size])
        sample_token_count = self.encoding(model=self.llm_model,
                                           text=sample_text)
        # Avoid division-by-zero; fallback to a 1:1 ratio if necessary.
        if sample_token_count == 0:
            words_per_token = 1.0
        else:
            words_per_token = sample_size / sample_token_count

        # 10% of the maximum tokens (based on the full allowance, not the chunk limit)
        extra_tokens_target = int(get_max_tokens(model=self.llm_model) * 0.1)
        # Estimate number of words that roughly corresponds to extra_tokens_target.
        estimated_word_step = max(1, int(extra_tokens_target * words_per_token))

        # Loop over words using estimated increments.
        while i < len(words):
            candidate_end = min(i + estimated_word_step, len(words))
            # Form a candidate by appending the block of words.
            candidate_chunk = current_chunk_words + words[i:candidate_end]
            candidate_text = " ".join(candidate_chunk)
            candidate_token_count = self.encoding(model=self.llm_model,
                                                  text=candidate_text)

            if candidate_token_count < chunk_token_limit:
                # The candidate block fits, so add it and advance.
                current_chunk_words.extend(words[i:candidate_end])
                i = candidate_end
            else:
                # The candidate block pushes us over the limit.
                # Use binary search to find the maximum number of words we can add.
                low = i
                high = candidate_end
                best = i  # This will mark the highest index we can use without exceeding the limit.
                count = 0
                max_count = 6
                while low <= high and count < max_count:
                    mid = (low + high) // 2
                    test_chunk = current_chunk_words + words[i:mid]
                    test_text = " ".join(test_chunk)
                    test_token_count = self.encoding(model=self.llm_model,
                                                     text=test_text)
                    if test_token_count <= chunk_token_limit:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                    count += 1

                # Append the maximum safe number of words found.
                current_chunk_words.extend(words[i:best])
                # Finalize the current chunk.
                chunk_text = " ".join(current_chunk_words)
                chunks.append(chunk_text)

                # Create an overlap using the last portion of the finalized chunk.
                overlap_target_tokens = int(chunk_token_limit * overlap_ratio)
                overlap_word_count = max(1,
                                         int(overlap_target_tokens * words_per_token))
                current_chunk_words = current_chunk_words[-overlap_word_count:]
                i = best

        # Append any leftover words as a final chunk.
        if current_chunk_words:
            final_chunk_text = " ".join(current_chunk_words)
            chunks.append(final_chunk_text)

        print(
            f"Generated {len(chunks)} chunks based on {self.llm_model} token limit.")
        return chunks

    import json

    def batch_extract_relationships_for_chunk(self, chunk_text, pairs):
        """
        Given a transcript chunk and a list of entity pairs (each as a tuple),
        this method sends one or more API calls to extract directional relationships.

        It first constructs the prompt from the instructions, the chunk of text, and
        the list of pairs. It then uses get_max_tokens and self.encoding to determine
        if the entire prompt fits within the token limit.

        If the prompt is too long (because of too many pairs) or if there are more than 50 pairs,
        the list of pairs is split into smaller groups. The API is called separately for each group,
        and the results (which include a "pair_index" corresponding to the original order in the pair list)
        are combined and returned.

        The expected JSON output from the API should contain a key "results" with a list of objects that have
        the keys "pair_index", "direction", and "relationship".
        """
        # The instructions remain as defined.
        instructions = (
            """You are an expert relation extractor.  Follow these rules exactly:
            1. For each entity pair determine which is the actor (subject) and which is the receiver (object). If roles are unclear, set direction to "none".
            2. Propose one or more precise predicates that describe distinct relationships between subject and object. Avoid vague verbs like “led to,” “part of,” or “caused by.”  
            3. If there are multiple meaningful relations, list up to three. Each must have its own direction and rationale.
            4. For each relation include a brief rationale (≤15 words) explaining your choice of subject, object, and predicate.
            5. Output exactly one JSON object of the form:
               {
                 "results": [
                   {
                     "pair_index": 1,
                     "direction": "forward",
                     "relationship": "fought against",
                     "rationale": "Nelson Mandela appears as grammatical subject and initiates the action"
                   },
                   {
                     "pair_index": 1,
                     "direction": "reverse",
                     "relationship": "surrendered to",
                     "rationale": "United States is the object receiving the action of surrender"
                   },
                   {
                     "pair_index": 2,
                     "direction": "reverse",
                     "relationship": "commander of",
                     "rationale": "Ulysses Grant is identified as leading Union's Army in the sentence"
                   },
                   {
                     "pair_index": 2,
                     "direction": "none",
                     "relationship": "",
                     "rationale": "No clear subject/object roles"
                   }
                 ]
               }
            6. Do not include any other keys or commentary."""
        )

        # Tag each pair with its original index for later reference.
        pairs_with_index = [(i, entity1, entity2) for i, (entity1, entity2) in
                            enumerate(pairs, start=1)]

        # Build the fixed portion of the prompt: instructions and context.
        base_prompt = (
                instructions
                + "\n"
                + f"Context:\n\"\"\"\n{chunk_text}\n\"\"\"\n\nPairs to process:\n"
        )

        # Helper function to produce pairs text.
        def generate_pairs_text(pairs_list):
            text = ""
            for idx, entity1, entity2 in pairs_list:
                text += f"{idx}. {entity1} -- {entity2}\n"
            return text

        # Get the maximum token limit.
        max_allowed_tokens = get_max_tokens(model=self.llm_model)
        base_token_count = self.encoding(model=self.llm_model, text=base_prompt)

        # Recursive helper: first ensure that no batch has more than 50 pairs,
        # then check if the full prompt fits within the token limit.
        def split_pairs_into_batches(pair_list):
            # Enforce a maximum of 50 pairs per batch.
            if len(pair_list) > EXTRACT_RELATIONSHIP_BATCH_SIZE:
                batches = []
                for i in range(0, len(pair_list), EXTRACT_RELATIONSHIP_BATCH_SIZE):
                    sub_batch = pair_list[i:i + EXTRACT_RELATIONSHIP_BATCH_SIZE]
                    batches.extend(split_pairs_into_batches(sub_batch))
                return batches

            pairs_text = generate_pairs_text(pair_list)
            full_prompt = base_prompt + pairs_text
            if self.encoding(model=self.llm_model,
                             text=full_prompt) <= max_allowed_tokens:
                return [pair_list]
            if len(pair_list) == 1:
                # Even a single pair is too long; return it as is.
                return [pair_list]
            # Otherwise, split the list in half and process recursively.
            mid = len(pair_list) // 2
            left_batches = split_pairs_into_batches(pair_list[:mid])
            right_batches = split_pairs_into_batches(pair_list[mid:])
            return left_batches + right_batches

        # Split pairs into groups satisfying both the token limit and 50-pair maximum.
        batches = split_pairs_into_batches(pairs_with_index)
        combined_results = []

        # For each batch, build the prompt and call the API.
        for batch in batches:
            pairs_text = generate_pairs_text(batch)
            prompt = base_prompt + pairs_text

            print("Batch Extraction Input:")
            print(prompt)

            try:
                response, _, _ = get_completion(
                    prompt,
                    model_name=self.llm_model,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    llm_api_key=self.llm_api_key,
                )
                raw_output = response.strip()
                print("Raw API response:")
                print(raw_output)
                json_str = extract_json(raw_output)
                try:
                    result = json.loads(json_str)
                except Exception as parse_error:
                    print("JSON parsing error:", parse_error)
                    result = {}
            except Exception as extraction_error:
                print("Error in batch extraction:", extraction_error)
                result = {}

            if isinstance(result, dict):
                if "results" in result:
                    combined_results.extend(result["results"])
                else:
                    print(
                        f"Error in response: expected dictionary with key \"results\" but got {result}"
                    )
            elif isinstance(result, list):
                combined_results.extend(result)
            else:
                print("Error: API response is not a dictionary nor list.")

        return combined_results

    def build_entity_relationships(self, transcript_str: str,
                                   unify_opposites=True,
                                   fallback_if_no_chunk=True):
        if self.A is None:
            raise ValueError(
                "Adjacency matrix not found. Run the coretexts segmentation first.")

        token_safety_margin = 600
        rows, cols = self.A.nonzero()
        unique_pairs = list({(self.keywords[r], self.keywords[c])
                             for r, c in zip(rows, cols) if r <= c})

        # first pass: overlapping chunks where both entities appear
        chunks = self.chunk_transcript_sliding(
            transcript_str, safety_margin=token_safety_margin)
        pair_results = {}
        for chunk in chunks:
            lower = chunk.lower()
            chunk_pairs = [(e1, e2) for (e1, e2) in unique_pairs
                           if e1.lower() in lower and e2.lower() in lower]
            if not chunk_pairs:
                continue
            batch = self.batch_extract_relationships_for_chunk(chunk, chunk_pairs)
            for res in batch:
                idx = res.get("pair_index", 0) - 1
                if not (0 <= idx < len(chunk_pairs)):
                    continue
                pair = chunk_pairs[idx]
                pair_results.setdefault(pair, []).append(
                    (res.get("direction", "none"), str(res.get("relationship", "")))
                )

        # fallback: pairs not found in any chunk
        fallback_pairs = [p for p in unique_pairs if p not in pair_results]
        if fallback_pairs and fallback_if_no_chunk:
            max_allowed = get_max_tokens(model=self.llm_model) - token_safety_margin
            # DEBUG: if entire transcript fits, use it directly
            if self.encoding(model=self.llm_model, text=transcript_str) <= max_allowed:
                results = self.batch_extract_relationships_for_chunk(
                    transcript_str, fallback_pairs)
                for res in results:
                    idx = res.get("pair_index", 0) - 1
                    if 0 <= idx < len(fallback_pairs):
                        pair = fallback_pairs[idx]
                        pair_results.setdefault(pair, []).append(
                            (res.get("direction", "none"), str(res.get("relationship", "")))
                        )
            else:
                # grouping using sentences mentioning either entity
                sentences = re.split(r'(?<=[.!?])\s+', transcript_str)
                pairIdxs = {}
                for pair in fallback_pairs:
                    e1, e2 = pair
                    idxs = [i for i, s in enumerate(sentences)
                            if e1.lower() in s.lower() or e2.lower() in s.lower()]
                    if idxs:
                        pairIdxs[pair] = idxs
                if pairIdxs:
                    sentenceToks = [self.encoding(model=self.llm_model, text=s)
                                    for s in sentences]

                    def expand(idxs):
                        s = set(idxs)
                        for i in idxs:
                            if i > 0: s.add(i-1)
                            if i < len(sentences)-1: s.add(i+1)
                        return s

                    def makeContext(idxs):
                        parts, prev = [], None
                        for i in sorted(idxs):
                            if prev is not None and i != prev+1:
                                parts.append("[...]")
                            parts.append(sentences[i])
                            prev = i
                        return " ".join(parts)

                    def groupPairs(pDict, toks, maxToks):
                        items = []
                        for pair, idxs in pDict.items():
                            ex = expand(idxs)
                            cost = sum(toks[i] for i in ex)
                            items.append((pair, ex, cost))
                        items.sort(key=lambda x: x[2], reverse=True)

                        groups = []
                        for pair, exSet, cost in items:
                            best = None
                            for g in groups:
                                if len(g["pairs"]) >= EXTRACT_RELATIONSHIP_BATCH_SIZE:
                                    continue
                                newSet = g["sentences"] | exSet
                                newCost = sum(toks[i] for i in newSet)
                                if newCost <= maxToks:
                                    added = newCost - g["tokenSum"]
                                    if best is None or added < best["added"]:
                                        best = {"group": g,
                                                "added": added,
                                                "newSet": newSet,
                                                "newCost": newCost}
                            if best:
                                grp = best["group"]
                                grp["pairs"].append(pair)
                                grp["sentences"] = best["newSet"]
                                grp["tokenSum"]  = best["newCost"]
                            else:
                                groups.append({
                                    "pairs": [pair],
                                    "sentences": exSet,
                                    "tokenSum": cost
                                })
                        return groups

                    groups = groupPairs(pairIdxs, sentenceToks, max_allowed)
                    for g in groups:
                        ctx = makeContext(g["sentences"])
                        batch = self.batch_extract_relationships_for_chunk(ctx, g["pairs"])
                        for res in batch:
                            idx = res.get("pair_index", 0) - 1
                            if 0 <= idx < len(g["pairs"]):
                                pair = g["pairs"][idx]
                                pair_results.setdefault(pair, []).append(
                                    (res.get("direction", "none"), str(res.get("relationship", "")))
                                )

        # consolidate into final edges list
        edges = []
        for pair in unique_pairs:
            rels = pair_results.get(pair, [("none", "")])
            seen, unified = set(), []
            for direction, rel in rels:
                key = (direction, rel.strip())
                if rel.strip() and key not in seen:
                    seen.add(key)
                    unified.append(key)
            for direction, rel in (unified or rels):
                edges.append((pair[0], rel, pair[1], direction))

        return edges

    @staticmethod
    def replace_labels(ind, labels):
        ind_new = np.zeros_like(ind)
        for i in range(len(labels)):
            ind_new[ind == i] = labels[i]
        return ind_new

    @staticmethod
    def ANN_search(X1, X2, k, similarity='euclidean'):
        M, d1 = X1.shape
        N, d2 = X2.shape
        assert d1 == d2, "Dimension mismatch."
        if similarity not in ['euclidean', 'angular', 'manhattan', 'hamming',
                              'dot']:
            sys.exit("Invalid similarity " + similarity)
        d = d1
        k = min(k, X2.shape[0])
        t = AnnoyIndex(d, similarity)
        for i in range(N):
            t.add_item(i, X2[i])
        t.build(5)
        knn_dist = []
        knn_ind = []
        for x1 in X1:
            indices, distances = t.get_nns_by_vector(x1, k,
                                                     include_distances=True)
            knn_ind.append(indices)
            knn_dist.append(distances)
        return np.array(knn_ind), np.array(knn_dist)
