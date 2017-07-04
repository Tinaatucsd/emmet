import logging
import numpy as np
from datetime import datetime
from itertools import combinations

from pymatgen import Structure
from pymatgen.analysis.piezo import PiezoTensor
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.core.operations import SymmOp

from maggma.builder import Builder
from emmet.vasp.builders.task_tagger import TaskTagger


__author__ = "Shyam Dwaraknath <shyamd@lbl.gov>"


class DielectricBuilder(Builder):
    def __init__(self, tasks, materials, dielectric, query={}, **kwargs):
        """
        Creates a dielectric collection for materials

        Args:
            tasks (Store): Store of task documents
            materials (Store): Store of materials documents to match to
            dielectric (Store): Store of dielectric properties
            query (dict): dictionary to limit materials to be analyzed
        """

        self.tasks = tasks
        self.materials = materials
        self.dielectric = dielectric
        self.query = query
        self.snls = snls

        self.logger = logging.getLogger(__name__).addHandler(logging.NullHandler())

        super().__init__(sources=[tasks, materials],
                         targets=[dielectric],
                         **kwargs)

    def get_items(self):
        """
        Gets all items to process into materials documents

        Returns:
            generator or list relevant tasks and materials to process into materials documents
        """

        self.logger.info("Dielectric Builder Started")
        q = dict(self.query)
        q.update(self.materials.lu_filter(self.dielectric))
        q["band_gap"] = {"$gt": 0.1}  # TODO: Consider smaller band gap?
        mats = self.materials.find(q, {"material_id": 1, "structure": 1, "task_ids": 1})

        self.logger.info("Found {} new materials for dielectric data".format(mats.count()))
        for mat in mats:
            tasks = list(self.tasks.find({"task_id": {"$in": mat["task_ids"]}}))

            yield {"material_id": mat["material_id"],
                   "structure": mat["structure"],
                   "tasks": tasks}

    def process_item(self, item):
        """
        Process the tasks and materials into a dielectrics collection

        Args:
            item dict: a dict of material_id, structure, and tasks

        Returns:
            dict: a dieletrics dictionary  
        """

        def is_centro(structure):
            sga = SpacegroupAnalyzer(structure)
            return SymmOp.inversion() in sga.get_symmetry_operations()

        def poly(matrix):
            diags = np.diagonal(matrix)
            return np.prod(diags) / np.sum(np.prod(comb) for comb in combinations(diags, 2))

        structure = Structure.from_dict(item["structure"])

        # Start with higher task ids first?
        for t in sorted(item["tasks"], lambda x: x[self.tasks.lu_field], reverse=True):
            if "dielectric" in TaskTagger.task_type(t):
                output = t["calcs_reversed"][0]["output"]
                total = np.sum(output["epsilon_ionic"], output["epsilon_static"])
                d = {"material_id": item['material_id']
                     "dielectric": {
                         "ionic": output["epsilon_ionic"],
                         "electronic": output["epsilon_static"],
                         "total": total,
                         "e_total": poly(total),
                         "e_ionic": poly(output["epsilon_ionic"]),
                         "e_electronic": poly(output["epsilon_static"]),
                     }
                     }

                # Update piezo if non_centrosymmetric
                if not is_centro(item["structure"]):
                    pt_e = PiezoTensor.from_voigt(np.array(output["outcar"]["piezo_tensor"]))
                    pt_i = PiezoTensor.from_voigt(np.array(output["outcar"]["piezo_ionic_tensor"]))
                    pt_total = pt_e + pt_i
                    pt_total = pt_total.symmetrized.fit_to_structure(structure)

                    d["piezo"] = {
                        "piezoelectric_tensor": pt_total.voigt,
                        "eij_max": np.max(pt_total.voigt),
                    }
                    # TODO Add in more analysis: v_max ?
                    # TODO: Add in unstable phonon mode analysis of piezoelectric for potentially ferroelectric

                return d

        return None

    def update_targets(self, items):
        """
        Inserts the new task_types into the task_types collection

        Args:
            items ([([dict],[int])]): A list of tuples of materials to update and the corresponding processed task_ids
        """

        items = [item in items if item is not None]

        self.logger.info("Updating {} dielectrics".format(len(items)))

        for doc in items:
            doc[self.dielectric.lu_field] = datetime.utcnow()
            self.dielectric.replace_one({"material_id": doc["material_id"]}, doc, upsert=True)

    def finalize(self):
        pass
