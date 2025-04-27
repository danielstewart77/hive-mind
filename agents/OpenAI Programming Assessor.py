from agent_tooling import tool
import openai

#@tool
def programming_assessment():
    '''
    This function conducts a programming assessment through a series of chat messages. It leverages OpenAI's API to generate questions, evaluate the user's responses, and provide feedback. The assessment includes 20 questions, increasingly difficult, and terminates after 3 consecutive failed responses by the user.
    '''
    openai.api_key = 'your_openai_api_key'

    def generate_questions():
        prompt = "Create 20 progressively advanced programming questions."
        response = openai.Completion.create(
            engine="text-davinci-003",
            prompt=prompt,
            max_tokens=500
        )
        questions = response.choices[0].text.strip().split('\n')
        return questions

    def assess_answer(question, user_answer):
        query = f"Question: {question}\nAnswer: {user_answer}\nEvaluate this response."
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": query}
            ],
            max_tokens=100
        )
        assessment = response.choices[0].message['content']
        return eval_assessment(assessment)

    def eval_assessment(assessment):
        try:
            # Assuming assessment returns in the correct format `correct: bool, reason: str`
            parts = assessment.split(', ')
            is_correct = 'True' in parts[0]
            reason = ''.join(parts[1:]).replace('reason: ', '')
            return is_correct, reason
        except Exception as e:
            return False, "Error in evaluation: " + str(e)

    questions = generate_questions()
    failed_consecutive_count = 0
    current_question_index = 0

    while current_question_index < 20:
        if failed_consecutive_count >= 3:
            print("Assessment ended after 3 consecutive failed attempts.")
            break

        current_question = questions[current_question_index]
        print(f"Question {current_question_index + 1}: {current_question}")

        user_answer = input("Your answer: ")
        correct, reason = assess_answer(current_question, user_answer)

        if correct:
            print("Correct! Reason:" + reason)
            failed_consecutive_count = 0
        else:
            print("Incorrect. Reason:" + reason)
            failed_consecutive_count += 1

        current_question_index += 1
    
    print("Assessment completed.")