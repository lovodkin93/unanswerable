import os
import pickle
import argparse
import logging
# Set the logging level to INFO
logging.basicConfig(level=logging.INFO)
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split, GridSearchCV
import torch
import pickle
from pathlib import Path

SEED = 42


def adapt_hidden_embeddings(instance, embedding_type):
    # if the embeddings of all the generation steps were saved in a single matrix, rather than in a list, separate them
    if len(instance[embedding_type][-1].shape) == 2:
        instance[embedding_type] = [instance[embedding_type][0][i,:] for i in range(instance[embedding_type][0].shape[0])]

    
    
    # removing the paddings
    # Compare all elements to 1
    matches = instance['all_outputs_ids'][0,:].eq(1)

    # Find the first non-zero element in matches
    indices = matches.nonzero(as_tuple=True)

    # Get the first index where value is 1 (if no 1 then no "padding" and so can take all embeddings)
    filter_index = indices[0][0].item() if indices[0].numel() != 0 else len(instance[embedding_type])

    filtered_hidden_embedding = instance[embedding_type][:filter_index]
    return filtered_hidden_embedding


def get_model_name(indir):
    if "UL2_Flan" in indir:
        curr_model = "UL2_Flan"
    elif "T5_xxl_Flan" in indir:
        curr_model = "T5_xxl_Flan"
    elif "OPT" in indir:
        curr_model = "OPT"
    else:
        raise Exception(f"curr model not found in indir: {indir}")
    return curr_model


def get_data(indir, prompt_type, embedding_type, dataset, num_instances, aggregation_type):
    data = dict()
    for file_name in os.listdir(indir):
        if not dataset in file_name or not file_name.endswith(".pt"):
            continue
        curr_data = torch.load(os.path.join(indir, file_name))

        if num_instances != None:
            curr_data = {key:value[:num_instances] for key,value in curr_data.items()}

        data_type = "control_group" if "control_group" in file_name else "adversarial"
        data[data_type] = curr_data

    if embedding_type == "first_hidden_embedding":
        adversarial_instances = [elem[embedding_type].mean(dim=0).cpu().numpy() for elem in data["adversarial"][prompt_type]]
        control_group_instances = [elem[embedding_type].mean(dim=0).cpu().numpy() for elem in data["control_group"][prompt_type]]
    elif aggregation_type == "average":
        adversarial_instances = [torch.stack(adapt_hidden_embeddings(elem, embedding_type)).mean(dim=0).cpu().numpy() for elem in data["adversarial"][prompt_type]]
        control_group_instances = [torch.stack(adapt_hidden_embeddings(elem, embedding_type)).mean(dim=0).cpu().numpy() for elem in data["control_group"][prompt_type]]
    elif aggregation_type == "union":
        adversarial_instances = [emb.cpu().numpy() for elem in data["adversarial"][prompt_type] for emb in adapt_hidden_embeddings(elem, embedding_type)]
        control_group_instances = [emb.cpu().numpy() for elem in data["control_group"][prompt_type] for emb in adapt_hidden_embeddings(elem, embedding_type)]
    elif aggregation_type == "only_first_tkn":
        adversarial_instances = [adapt_hidden_embeddings(elem, embedding_type)[0].cpu().numpy() for elem in data["adversarial"][prompt_type]]
        control_group_instances = [adapt_hidden_embeddings(elem, embedding_type)[0].cpu().numpy() for elem in data["control_group"][prompt_type]]
    else:
        raise Exception("--aggregation-type did not receive a valid option. Only one of 'average', 'union' or 'only_first_tkn'")

    return adversarial_instances, control_group_instances



def main(args):
    indir = args.indir
    outdir = args.outdir
    dataset = args.dataset
    prompt_type = args.prompt_type
    embedding_type = args.embedding_type
    aggregation_type = args.aggregation_type
    num_instances = args.num_instances
    model_name = get_model_name(indir)
    outdir = os.path.join(outdir, dataset, embedding_type, prompt_type, aggregation_type, f"{model_name}_{num_instances}N")

    # create outdir
    folder_path = Path(outdir)
    folder_path.mkdir(parents=True, exist_ok=True)
    print(f"classifier saved to {outdir}")

    # get data
    adversarial_instances, control_group_instances = get_data(indir, prompt_type, embedding_type, dataset, num_instances, aggregation_type)

    # Combine the instances and create corresponding labels
    adversarial_labels = np.zeros(len(adversarial_instances))
    control_group_labels = np.ones(len(control_group_instances))

    X = np.concatenate((adversarial_instances, control_group_instances))
    y = np.concatenate((adversarial_labels, control_group_labels))

    # Split data into train, validation, and test sets (60% train, 20% validation, 20% test)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=SEED)



    # Train a linear classifier using logistic regression
    clf = LogisticRegression(random_state=SEED)


    param_grid = {
            'C': [0.0001, 0.001, 0.01, 0.1, 1, 10, 100, 1000],
            'penalty': ['l1', 'l2'],
            'solver': ['liblinear'],
            'max_iter': [1000, 5000]
        }


    grid_search = GridSearchCV(clf, param_grid, cv=5, scoring='accuracy', refit=True)
    grid_search.fit(X_train, y_train)



    # Log the accuracy of different hyperparameters in the GridSearch
    print("GridSearchCV results:")
    for mean_score, std_score, params in zip(grid_search.cv_results_['mean_test_score'], grid_search.cv_results_['std_test_score'], grid_search.cv_results_['params']):
        print(f"Mean accuracy: {mean_score:.4f}, Std: {std_score:.4f}, Parameters: {params}")


    # save model
    model_filename = os.path.join(outdir, "best_model.pkl")
    with open(model_filename, 'wb') as file:
        pickle.dump(grid_search.best_estimator_, file)

    print(f"Best model saved to {model_filename}")






if __name__ == '__main__':
    argparser = argparse.ArgumentParser(description="")
    argparser.add_argument('-i', '--indir', type=str, required=True, help='path to data')
    argparser.add_argument('-o', '--outdir', type=str, required=True, help='path to outdir')
    argparser.add_argument('--dataset', type=str, default="squad", help='prompt type to classify ("squad", "NQ", "musique")')
    argparser.add_argument('--prompt-type', type=str, default="Adversarial", help='prompt type to classify ("Adversarial", "Pseudo-Adversarial", "CoT-Adversarial", "Answerability")')
    argparser.add_argument('--epochs', type=int, default=500, help='number of epochs')
    argparser.add_argument('--batch-size', type=int, default=32, help='batch size of train set.')
    argparser.add_argument('--eval-batch-size', type=int, default=64, help='batch size of dev and test sets.')
    argparser.add_argument('--num-instances', type=int, default=None, help='number of instances to use for training (will take the same amount from the control_group and the adversarial). If None - will take all.')
    argparser.add_argument('--save-interval', type=int, default=10, help='how frequently to save model')
    argparser.add_argument('--eval-interval', type=int, default=10, help='how frequently to evaluate on the devset (in epochs)')
    argparser.add_argument('--aggregation-type', type=str, default="only_first_tkn", help='how to aggregate all the hidden layers of all the generated tokens of a single instance (choose from "average" to average them, "union" to treat each of them as an instance, and "only_first_tkn" to only take the first token\'s hidden layers).')
    argparser.add_argument('--embedding-type', type=str, default="last_hidden_embedding", help='which layer to take: any one of "last_hidden_embedding" and "first_hidden_embedding"')
    args = argparser.parse_args()
    main(args)


