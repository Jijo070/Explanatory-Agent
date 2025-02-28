"""
WHAT: A class to manage the case solutions(s) found in the database.
WHY: Need to separate concerns.
ASSUMES: None
FUTURE IMPROVEMENTS: N/A
WHO: SL 2021-05-12


For each triplet:
  get solutions

trim all solution lists to smallest solution list length

for i = 0 i < smallest solution list length:
    for each triplet (again):
      run solution i

"""

from modDatabase import db
from .clsCaseSolution import clsCaseSolution
from .clsCaseSolutionPath import clsCaseSolutionPath
from .clsKnowledgeProvider import clsKnowledgeProvider
from .clsQueryPath import clsQueryPath
from .clsMultiHop import clsMultiHop
from utils.multithreading.clsNode import clsNode
from utils.modMiscUtils import isNullOrEmptyList
from .clsBioLinkSimilarity import clsBiolinkSimilarity
from .clsCaseSolutionRetrival import clsCaseSolutionRetrival
from ..models.workflow.clsWorkflow import Workflow
from ..models.workflow.clsOperations import FillOperation
from .clsKnowledgeType import clsKnowledgeType
import copy
from utils.clsLog import clsLogEvent
from modConfig import environmentModeKPURLColumn, environmentMode
import logging


class clsCaseSolutionManager(clsNode):
    """
    See header
    """


    sqlGenerateSimilarityScore = \
        """
        select
            x."Similarity_score"
            from "xARA_LocalSimNodes" x
            where 
            x."Node_1"=:node1
            and x."Node_2"=:node2
        """

    sqlFindMostSimilarCases = \
        """
        with FilteredGlobalSimilarity as (
            select
                g.*
            from "v1_1_GlobalSimilarity" g
            where g."Subject" in :similarSubjectsVar
            and g."Object" in :similarObjectsVar
            and g."Predicate" in :similarPredicatesVar  --this is only actual 1 predicate by design
            and g."CaseValue" > 0 --make sure we don't have dissimilar
            order by
                g."CaseValue" desc,
                g."Id" asc --if theres duplicate case values
        )
        
        select
            distinct
            f."CaseId"
        from FilteredGlobalSimilarity f
        limit 2;
        """

    sqlFindCaseSolutions = \
        """
        select
            A."SOLUTION_ID", A."CASE_ID",
            A."SOLUTION_FIRST_KP_NAME" as "KP_PATH1", A."NODE1_PATH1_CATEGORY", A."NODE2_PATH1_CATEGORY", A."EDGE1_PATH1_PREDICATE",
            A."SOLUTION_SECOND_KP_NAME" as "KP_PATH2", A."NODE1_PATH2_CATEGORY", A."NODE2_PATH2_CATEGORY", A."EDGE1_PATH2_PREDICATE",
        case
            when "xARA_KP_Info"."Priority" IS NULL THEN 0
            else "xARA_KP_Info"."Priority" END AS "Priority" 
        from( 
            select 
                "SOLUTION_ID", "CASE_ID", 
                "SOLUTION_FIRST_KP_NAME", "NODE1_PATH1_CATEGORY", "NODE2_PATH1_CATEGORY", "EDGE1_PATH1_PREDICATE",
                "SOLUTION_SECOND_KP_NAME", "NODE1_PATH2_CATEGORY", "NODE2_PATH2_CATEGORY", "EDGE1_PATH2_PREDICATE"
            from "xARA_CaseSolutions" 
            where "CASE_ID" = ANY(array[:caseIds])
        )
        A LEFT OUTER JOIN "xARA_KP_Info"
        ON A."SOLUTION_FIRST_KP_NAME" = "xARA_KP_Info"."KP_Name"
        """

    def __init__(self, dispatchId: int, dispatchDescription: str, userRequestBody: dict, workflow: Workflow):
        """
        Constructor
        :param userRequestBody: the request body the user sends
        """

        super().__init__(dispatchId=dispatchId, dispatchDescription=dispatchDescription, dispatchMode="parallel", dispatchList=[])
        self.userRequestBody = userRequestBody

        self.similarSubjects = None
        self.similarObjects = None
        self.similarPredicates = None

        self.subjectCurieIds = None
        self.objectCurieIds = None

        self.edgeKnowledgeType = None
        self.edgeAttributeConstraints = None
        self.edgeQualifierConstraints = None
        self.subjectConstraints = None
        self.objectConstraints = None

        self.caseIds = None

        # outputs
        self.query_graph = None
        self.knowledge_graph = None
        self.results = None

        self.logs = None

        self.default_case_similarity = None
        # stored here to be passed to the cases
        self.default_explanation_similarity = None

        # pass current_app._get_current_object() as reference inside threading
        self.app = None
        # class that finds a case problem list for a subject, predicate, and object
        self.case_problem_searcher = None

        self.kp_urls = None

        self.one_hop_origin = ["fromKP", "derived"]
        self.multiple_hop_origin = ["fromKP"]

        # set a flag if query graph is "one-hop" (i.e. [node]-[edge]->[node])
        self.is_one_hop = len(dispatchDescription["nodes"]) == 2 and len(dispatchDescription["edges"]) == 1

        self.workflow: Workflow = workflow

        # As per Explanatory Agent Creative Mode - Workflow Update Request from 2023-02-15
        # This flag will only be set if, during a creative mode query, no results have been found for executed derived case problems.
        # This flag will force the case problem searcher to only look for fromKP solutions (and perform all other logic associated with that step)
        self.creative_no_results_override: bool = False
        # Specifies if a creative mode query has already selected fromKP results, meaning there were no derived cases that satisfied the search. This is used
        # to skip rerunning this manager if no results were returned
        self.creative_searched_from_kps: bool = False

    def initialize_db_data(self):
        """
        Gathers all data that is needed from the database
        :return:
        """
        self.retrieve_kp_urls()

    def retrieve_kp_urls(self):
        """

        :return:
        """
        self.kp_urls = {}

        # Select the current environment mode as the primary URL column to request, failing over to more mature URLs if there is no URL present.
        # Order is currently Development -> Staging (CI) -> Testing -> Prod
        for environment_mode_index, mode_label in enumerate(environmentModeKPURLColumn.keys()):
            if mode_label == environmentMode:
                break
        else:
            environment_mode_index = -1

        coalesce_arguments = []
        for column_name in list(environmentModeKPURLColumn.keys())[environment_mode_index:]:
            coalesce_arguments.append(f"""NULLIF("{environmentModeKPURLColumn[column_name]}",'')""")
        query = f"""
        SELECT "KP_Name", COALESCE({", ".join(coalesce_arguments)}) as "URL" FROM public."xARA_KP_Info";
        """

        with self.app.app_context():
            results = db.session.execute(query).fetchall()

        for result in results:
            kp_name = result[0]
            url = result[1]
            self.kp_urls[kp_name] = url

    def extractMetadataFromUserRequestBody(self):

        # extract edge metadata
        edges = self.userRequestBody['message']['query_graph']['edges']
        edge = edges[list(edges.keys())[0]]

        self.edgeKnowledgeType = edge.get('knowledge_type', None)
        self.edgeAttributeConstraints = edge.get('attribute_constraints', None)
        self.edgeQualifierConstraints = edge.get('qualifier_constraints', None)

        # extract nodes metadata
        nodes = self.userRequestBody['message']['query_graph']['nodes']
        subjectNode = nodes[edge['subject']]
        objectNode = nodes[edge['object']]

        self.similarSubjects = subjectNode['categories']
        self.similarObjects = objectNode['categories']

        if 'ids' in subjectNode:
            self.subjectCurieIds = subjectNode['ids']
        if 'ids' in objectNode:
            self.objectCurieIds = objectNode['ids']

        if 'constraints' in subjectNode:
            self.subjectConstraints = subjectNode['constraints']
        if 'constraints' in objectNode:
            self.objectConstraints = objectNode['constraints']

    def calculateQueryPath(self):
        self.query_path = clsQueryPath(self.userRequestBody["message"]["query_graph"])
        self.query_path.create_plan()

    def findMostSimilarCases(self):
        self.triplets_case_ids = []
        for i, triplet in enumerate(self.query_path.triplets):
            source_category = triplet.source_node.data["categories"][0]
            target_category = triplet.target_node.data["categories"][0]
            predicate = triplet.predicate.data["predicates"][0]
            logging.debug(f"Finding similar cases for {triplet}")

            knowledge_type = triplet.predicate.data.get('knowledge_type', clsKnowledgeType.LOOKUP)
            origins = clsKnowledgeType.origins(knowledge_type)

            if self.creative_no_results_override is False:
                self.logs.append(clsLogEvent(
                    identifier=f"Hop {i + 1} of {len(self.query_path.triplets)} - {triplet}",
                    level="DEBUG",
                    code="",
                    message=f"Searching for cases in '{knowledge_type}' mode. Searching for {origins} cases."
                ))

                triplet_case_ids = self.case_problem_searcher.get_global_sim_triplets(source_category, target_category, predicate, origins, knowledge_type=knowledge_type)
            else:
                triplet_case_ids = []

            # As per Explanatory Agent Creative Change (2023-01-23 3:05 pm)
            # For xARA's Creative Mode ONLY:
            # Make the retrieval use only cases of origin "derived" , and only when there are no case solutions available from derived cases, then initiate the retrieval using “fromKP” cases.
            # The retrieval of derived cases would then use the same thresholds we used for “fromKP” cases previously.
            # Addendum 2023-01-31: When selecting top cases, exclude cases where global similarity score == 1
            # Addendum 2023-02-15: This logic will also be triggered if the creative_no_results_override flag is set. See the property definition in __init__() for details.
            if knowledge_type == clsKnowledgeType.CREATIVE_MODE and (len(triplet_case_ids) <= 0 or self.creative_no_results_override):
                origins = clsKnowledgeType.origins(clsKnowledgeType.LOOKUP)

                # the log message is slightly different if we are going directly to fromKP due to creative_no_results_override
                if self.creative_no_results_override is False:
                    log_message = f"No cases found, switching to {origins} cases."
                else:
                    log_message = f"Searching using {origins} cases."
                self.logs.append(clsLogEvent(
                    identifier=f"Hop {i + 1} of {len(self.query_path.triplets)} - {triplet}",
                    level="DEBUG",
                    code="",
                    message=log_message
                ))

                triplet_case_ids = self.case_problem_searcher.get_global_sim_triplets(source_category, target_category, predicate, origins,
                                                                                      knowledge_type=clsKnowledgeType.LOOKUP, max_similarity_lt=1.0)
                self.creative_searched_from_kps = True

            self.logs.append(clsLogEvent(
                identifier=f"Hop {i+1} of {len(self.query_path.triplets)} - {triplet}",
                level="DEBUG",
                code="",
                message=f"Identified {len(self.case_problem_searcher.selected_case_ids_and_similarities)} Case Problems: (Case ID, Similarity) '{self.case_problem_searcher.selected_case_ids_and_similarities}'"
            ))
            logging.debug(f"Identified {len(self.case_problem_searcher.selected_case_ids_and_similarities)} Case Problems: (Case ID, Similarity) '{self.case_problem_searcher.selected_case_ids_and_similarities}'")
            self.triplets_case_ids.append(triplet_case_ids)

    def getKPURL(self, kp_name):
        """
        Returns a URL for the specified KP. Currently a fixed list, must be updated to use smart-api.info for dynamic reading.
        :param kp_name: String to look up in smart-api
        :return: URL string
        """
        return self.kp_urls.get(kp_name, None)

    def populateResultKPInfo(self, result):
        new_result = {}
        for key in result.keys():
            new_result[key] = result[key]

        new_result["Id"] = result["SOLUTION_ID"]
        new_result["CaseId"] = result["CASE_ID"]
        new_result["Node1Path1Category"] = result["NODE1_PATH1_CATEGORY"]
        new_result["Node2Path1Category"] = result["NODE2_PATH1_CATEGORY"]
        new_result["Edge1Path1Predicate"] = result["EDGE1_PATH1_PREDICATE"]
        new_result["KnowledgeProviderPath1Name"] = result["KP_PATH1"]
        new_result["KnowledgeProviderPath1Url"] = self.getKPURL(result["KP_PATH1"])

        new_result["Node1Path2Category"] = result["NODE1_PATH2_CATEGORY"]
        new_result["Node2Path2Category"] = result["NODE2_PATH2_CATEGORY"]
        new_result["Edge1Path2Predicate"] = result["EDGE1_PATH1_PREDICATE"]
        new_result["KnowledgeProviderPath2Name"] = result["KP_PATH2"]
        new_result["KnowledgeProviderPath2Url"] = self.getKPURL(result["KP_PATH2"])

        if new_result["KP_PATH2"] is None:
            new_result["KnowledgeProviderPathCount"] = 1
        else:
            new_result["KnowledgeProviderPathCount"] = 2

        return new_result

    def buildCaseSolutionFromResult(self, triplet, caseSolutionDispatchId, result):

        knowledgeProviderPathCount = result["KnowledgeProviderPathCount"]
        if knowledgeProviderPathCount not in [1, 2]:
            raise AttributeError("Number of knowledge provider paths supported is either 1 or 2")

        caseSolution = clsCaseSolution(
            dispatchId=caseSolutionDispatchId,
            dispatchDescription=f"Solution {result['Id']} for Case {result['CaseId']}"
        )
        caseSolution.dispatchIdSuffix = "" if caseSolutionDispatchId == 1 else ("-" + str(caseSolutionDispatchId))
        caseSolution.logs = []
        # caseSolution.logs = self.logs  # enables shared log across all objects by passing by reference. Don't want currently.
        caseSolution.id = result["Id"]
        caseSolution.caseId = result["CaseId"]

        caseSolution.subject_query_graph_node_id = triplet.source_node.id
        caseSolution.object_query_graph_node_id = triplet.target_node.id
        caseSolution.predicate_query_graph_edge_id = triplet.predicate.id

        caseSolution.similarSubject = triplet.source_node.data["categories"]
        caseSolution.similarObject = triplet.target_node.data["categories"]
        caseSolution.similarPredicate = triplet.predicate.data["predicates"]
        caseSolution.subjectCurieIds = triplet.source_node.data.get("ids", None)
        caseSolution.objectCurieIds = triplet.target_node.data.get("ids", None)
        caseSolution.subjectConstraints = triplet.source_node.data.get("constraints", None)
        caseSolution.objectConstraints = triplet.target_node.data.get("constraints", None)
        caseSolution.edgeKnowledgeType = triplet.predicate.data.get("knowledge_type", None)
        caseSolution.edgeAttributeConstraints = triplet.predicate.data.get("attribute_constraints", None)
        caseSolution.edgeQualifierConstraints = triplet.predicate.data.get("qualifier_constraints", None)

        self.edgeKnowledgeType = None
        self.edgeAttributeConstraints = None
        self.edgeQualifierConstraints = None
        caseSolution.explanation_similarity = self.default_explanation_similarity
        caseSolution.explanation_solution_finder = self.explanation_solution_finder

        caseSolutionPath1 = clsCaseSolutionPath()
        caseSolutionPath1.logs = []
        caseSolutionPath1.subject = result["Node1Path1Category"]
        caseSolutionPath1.object = result["Node2Path1Category"]
        caseSolutionPath1.predicate = result["Edge1Path1Predicate"]
        caseSolutionPath1.triplet = triplet
        caseSolutionPath1.knowledgeProvider = clsKnowledgeProvider(
            name=result["KnowledgeProviderPath1Name"],
            url=result["KnowledgeProviderPath1Url"]
        )
        caseSolutionPath1.knowledgeProvider.logs = caseSolutionPath1.logs

        caseSolution.paths = [caseSolutionPath1]

        if knowledgeProviderPathCount == 2:
            caseSolutionPath2 = clsCaseSolutionPath()
            caseSolutionPath2.logs = []
            caseSolutionPath2.subject = result["Node1Path2Category"]
            caseSolutionPath2.object = result["Node2Path2Category"]
            caseSolutionPath2.predicate = result["Edge1Path2Predicate"]
            caseSolutionPath2.triplet = triplet
            caseSolutionPath2.knowledgeProvider = clsKnowledgeProvider(
                name=result["KnowledgeProviderPath2Name"],
                url=result["KnowledgeProviderPath2Url"]
            )
            caseSolutionPath2.knowledgeProvider.logs = caseSolutionPath2.logs

            caseSolution.paths.append(caseSolutionPath2)

        return caseSolution

    def filter_case_solutions(self, solutions):
        """
        Removes redundant case solutions: remove all solutions that use the same KP as another and have ontologically similar node types.
        :param solutions: List of tuples
        :return:
        """
        # TODO: Load conflations from DB:
        # SELECT "NEW_CASE_NODE", "CANDIDATE_CASE_NODE", "SIMILARITY_SCORE" FROM public."xARA_LocalSimNodes" WHERE "SIMILARITY_SCORE" > 0.9 AND "NEW_CASE_NODE" != "CANDIDATE_CASE_NODE";

        CONFLATIONS = {
            "biolink:Gene": {"biolink:Protein"},
            "biolink:Protein": {"biolink:Gene"},
            "biolink:ChemicalEntity": {"biolink:SmallMolecule"},
            "biolink:SmallMolecule": {"biolink:ChemicalEntity"},
        }
        PRIORITIES = [
            ["biolink:SmallMolecule", "biolink:ChemicalEntity"],
            ["biolink:Protein", "biolink:Gene"]
        ]

        # first filter the results to those that only have one hop and are the same KP
        excluding_solutions = list(filter(lambda row: row["KP_PATH2"] is None, solutions))
        # filter cases that don't have any potential conflations
        excluding_solutions = list(filter(lambda row: row["NODE1_PATH1_CATEGORY"] in CONFLATIONS or row["NODE2_PATH1_CATEGORY"] in CONFLATIONS, excluding_solutions))

        duplicate_solutions = {}

        # only compare those that have the same KP
        for i, solution_a in enumerate(excluding_solutions):
            for j, solution_b in enumerate(excluding_solutions):
                if solution_a["SOLUTION_ID"] != solution_b["SOLUTION_ID"] and solution_a["KP_PATH1"] == solution_b["KP_PATH1"]:
                    if solution_a["KP_PATH1"] not in duplicate_solutions:
                        duplicate_solutions[solution_a["KP_PATH1"]] = set()
                    duplicate_solutions[solution_a["KP_PATH1"]].add(solution_a)
                    duplicate_solutions[solution_a["KP_PATH1"]].add(solution_b)

        excluded_solutions = set()
        for kp, conflated_solutions in duplicate_solutions.items():
            if len(conflated_solutions) > 1:
                # of all the solutions, select the one that has the lowest index in PRIORITIES, meaning it is most specific
                solution_subject_priorities = {}
                solution_object_priorities = {}
                for solution in conflated_solutions:
                    for priority_set in PRIORITIES:
                        if solution["NODE1_PATH1_CATEGORY"] in priority_set:
                            subject_index = priority_set.index(solution["NODE1_PATH1_CATEGORY"])
                            if subject_index > -1:
                                solution_subject_priorities[solution] = subject_index

                        if solution["NODE2_PATH1_CATEGORY"] in priority_set:
                            object_index = priority_set.index(solution["NODE2_PATH1_CATEGORY"])
                            if object_index > -1:
                                solution_object_priorities[solution] = object_index

                if len(solution_subject_priorities) > 0:
                    lowest_solution = min(solution_subject_priorities, key=solution_subject_priorities.get)
                elif len(solution_object_priorities) > 0:
                    lowest_solution = min(solution_object_priorities, key=solution_object_priorities.get)
                else:
                    lowest_solution = list(conflated_solutions)[0]

                # keep the lowest solution, discard all others
                for solution in conflated_solutions:
                    if solution != lowest_solution:
                        excluded_solutions.add(solution)

        filtered_solutions = list(filter(lambda row: row not in excluded_solutions, solutions))

        return filtered_solutions

    def findCaseSolutions(self):
        case_solution_sorter = clsCaseSolutionRetrival()

        # if isNullOrEmptyList(self.caseIds): return

        all_case_ids = set()
        for case_ids in self.triplets_case_ids:
            all_case_ids |= set(case_ids)

        # create a white or blacklist for KPs if there is a Fill operation in the workflow.
        kp_allow_list = None
        kp_deny_list = None
        for operation in self.workflow.operations:
            if isinstance(operation, FillOperation):
                kp_allow_list = operation.allow_list
                kp_deny_list = operation.deny_list

        case_id_to_solutions = {}
        with self.app.app_context():
            # add a WHERE clause to the solution retrieval query to select a subset based on the Fill operation
            find_case_solutions = copy.copy(self.sqlFindCaseSolutions)
            if kp_allow_list:
                kp_list = kp_allow_list
                find_case_solutions += """ WHERE A."SOLUTION_FIRST_KP_NAME" = ANY(array[:kpList])"""
                logging.debug(f"Using allow list: {kp_list}")
            elif kp_deny_list:
                kp_list = kp_deny_list
                find_case_solutions += """ WHERE NOT (A."SOLUTION_FIRST_KP_NAME" = ANY(array[:kpList]))"""
                logging.debug(f"Using deny list: {kp_list}")
            else:
                kp_list = None
                logging.debug(f"No fill list provided, retrieving all solutions")

            results = db.session.execute(
                statement=find_case_solutions,
                params={
                    "caseIds": list(all_case_ids),
                    "kpList": kp_list
                }
            ).fetchall()
            logging.debug(f"Matched {len(results)} Case Solutions.")

            self.logs.append(clsLogEvent(
                identifier=f"Case Solution Manager",
                level="DEBUG",
                code="",
                message=f"Matched {len(results)} Case Solutions with {'allow' if kp_allow_list else 'deny'} list: {kp_list}"
            ))

            filtered_results = self.filter_case_solutions(results)

            self.logs.append(clsLogEvent(
                identifier=f"Case Solution Manager",
                level="DEBUG",
                code="",
                message=f"Filtered {len(results) - len(filtered_results)} conflated Case Solutions: {[solution['SOLUTION_ID'] for solution in sorted(list(set(results) - set(filtered_results)))]}"
            ))

            for result in filtered_results:
                case_id = result[1]
                if case_id not in case_id_to_solutions:
                    case_id_to_solutions[case_id] = []
                case_id_to_solutions[case_id].append(result)

        caseSolutionDispatchId = 1

        triplets_case_solutions = []
        for i, triplet in enumerate(self.query_path.triplets):
            case_ids = self.triplets_case_ids[i]
            triplet_case_solutions = []

            for caseId in case_ids:
                results = case_id_to_solutions.get(caseId, [])

                # logging.debug(f"Getting case solutions for Case ID {caseId}")
                if isNullOrEmptyList(results):
                    logging.error(f"No case solutions retrieved for Case ID '{caseId}")
                    continue

                sorted_results = case_solution_sorter.retrieveRows_xARA_CaseSolutions(results)

                for result in sorted_results:
                    triplet_case_solutions.append(
                        self.buildCaseSolutionFromResult(
                            triplet,
                            caseSolutionDispatchId=self.dispatchId + caseSolutionDispatchId,
                            result=self.populateResultKPInfo(result)
                        )
                    )
                    caseSolutionDispatchId += 1

            triplets_case_solutions.append(triplet_case_solutions)

            # source_category = triplet.given_node.data["categories"][0]
            # opposite_category = triplet.opposite_node.data["categories"][0]
            # predicate = triplet.predicate.data["predicates"][0]
            self.logs.append(clsLogEvent(
                identifier=f"Hop {i+1} of {len(self.query_path.triplets)} - {triplet}",
                level="DEBUG",
                code="",
                message=f"Identified {len(triplet_case_solutions)} Case Solutions (first 50): '{triplet_case_solutions[:50]}...'"
            ))
            logging.debug(f"Identified {len(triplet_case_solutions)} Case Solutions (first 50): '{triplet_case_solutions[:50]}...'")
        self.triplets_case_solutions = triplets_case_solutions

        # trim the solutions down to the smallest set size so they can be iterated with no missing steps
        smallest_solutions = min([len(s) for s in self.triplets_case_solutions])
        logging.debug(f"Reducing triplet solutions length to {smallest_solutions}")
        for i in range(len(self.triplets_case_solutions)):
            self.triplets_case_solutions[i] = self.triplets_case_solutions[i][:smallest_solutions]

        self.dispatchList = []
        for solution_index in range(len(self.triplets_case_solutions[0])):
            path_solutions = []
            for triplet_index, triplet in enumerate(self.query_path.triplets):
                triplet_case_solution = self.triplets_case_solutions[triplet_index][solution_index]
                path_solutions.append(triplet_case_solution)
            multi_hop = clsMultiHop(self.dispatchId + 10000 * (solution_index+1) + 1000 * (triplet_index + 1), "", path_solutions)
            self.dispatchList.append(multi_hop)

    def preExecute(self):
        self.extractMetadataFromUserRequestBody()
        self.calculateQueryPath()
        self.findMostSimilarCases()
        self.findCaseSolutions()
        if isNullOrEmptyList(self.dispatchList):
            self.dispatchList = None

    def execute(self):
        self.applyThreadLockToChildren()
        self.preExecute()
        # self.dispatch()  # no longer dispatching here, done in query manager
